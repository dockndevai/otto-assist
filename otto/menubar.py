"""Otto as a macOS menu bar app.

The AppKit run loop needs the main thread, so rumps runs there while the assistant
(orchestrator + brain + dashboard server) runs on its own asyncio loop in a background
thread. Menu actions on the main thread poke that loop via thread-safe calls; a 1s timer
reads the orchestrator's live state to keep the menu label current.

Run:  python -m otto.menubar
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser

import rumps
import uvicorn

# launchd job label. Quitting must unload it, or the LaunchAgent's KeepAlive immediately
# relaunches the process we just exited. The label depends on who installed us:
# scripts/install_service.sh uses "com.otto.assistant"; `brew services` uses
# "homebrew.mxcl.otto". Each plist exports OTTO_LAUNCHD_LABEL so Quit boots out the right one.
_LAUNCHD_LABEL = os.environ.get("OTTO_LAUNCHD_LABEL", "com.otto.assistant")

from .config import load_env, load_settings
from .orchestrator import Orchestrator
from .projects import list_projects
from .sessions import list_sessions
from .server.app import create_app
from .server.events import EventBus

_STATE_LABELS = {
    "starting": "Starting…",
    "idle": "● Ready",
    "listening": "● Listening…",
    "thinking": "● Thinking…",
    "speaking": "● Speaking…",
    "working": "● Working…",
    "sleeping": "☾ Sleeping — say “Otto”",
}


_ASSETS = os.path.join(os.path.dirname(__file__), "assets")


def _icon(name: str) -> str | None:
    p = os.path.join(_ASSETS, f"mb_{name}.png")
    return p if os.path.exists(p) else None


# Which colored robot to show for each live state (grey idle, colors when active).
_STATE_ICON = {
    "starting": "idle", "idle": "idle", "listening": "listening",
    "thinking": "thinking", "working": "thinking", "speaking": "speaking",
    "sleeping": "idle",
}


# -- single-instance guard + orphan sweep --------------------------------------
# The #1 cause of "Orrin stopped listening" is TWO instances running at once (a Dock
# double-click on top of the launcher, or leftover STT workers from a crashed run): they
# fight over the microphone and the recognizer's worker pipe dies (EOFError flood). A flock
# guarantees a single MAIN, and a startup sweep clears orphaned multiprocessing workers that
# reparented to launchd after a prior crash and would otherwise collide with the new recorder.
_lock_fd = None   # kept open for the process lifetime so the flock is held


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True     # exists, just not ours to signal
    except Exception:
        return False


def _acquire_single_instance_lock_or_exit() -> None:
    global _lock_fd
    from .config import OTTO_HOME
    lock_path = OTTO_HOME / "orrin.lock"
    try:
        OTTO_HOME.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    fd = open(lock_path, "a+")   # Python opens fds non-inheritable, so children can't hold it
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Locked. If the recorded MAIN pid is dead, this is a ghost hold (e.g. an orphaned
        # worker that inherited the fd) — sweep and retry once rather than refuse to start.
        other = ""
        try:
            fd.seek(0)
            other = fd.read().strip()
        except Exception:
            pass
        stale = other.isdigit() and not _pid_alive(int(other))
        if stale:
            _sweep_orphan_workers()
            time.sleep(0.5)
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print(f"[otto] Orrin is already running{f' (pid {other})' if other else ''}; "
                  "this instance is exiting to avoid a microphone collision.",
                  file=sys.stderr, flush=True)
            sys.exit(0)
    try:
        fd.seek(0)
        fd.truncate()
        fd.write(str(os.getpid()))
        fd.flush()
    except Exception:
        pass
    _lock_fd = fd   # hold the lock until the process dies (fd close releases it)


# Path markers that identify OUR multiprocessing workers (not some unrelated python).
_OTTO_PROC_MARKERS = ("personal-assist/.venv", "Orrin.app/Contents/MacOS",
                      "Application Support/Otto/venv", "otto-assist")


def _is_otto_worker(cmd: str) -> bool:
    """True if this process command line is one of OUR RealtimeSTT multiprocessing workers.
    The EXECUTABLE (argv[0]) must be the otto python / Orrin binary, so a shell/editor/grep that
    merely mentions the path isn't matched."""
    if "multiprocessing" not in cmd:
        return False
    exe = cmd.split(" ", 1)[0]
    return any(m in exe for m in _OTTO_PROC_MARKERS)


def _sweep_orphan_workers() -> int:
    """Kill leftover RealtimeSTT multiprocessing workers from a prior crashed/duplicate run.
    Safe to call before we spawn our own recorder — every match is an orphan by then. Returns
    the count killed."""
    me = os.getpid()
    try:
        out = subprocess.run(["ps", "-axo", "pid=,command="],
                             capture_output=True, text=True, timeout=5)
    except Exception:
        return 0
    killed = 0
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_s, _, cmd = line.partition(" ")
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        if pid == me or not _is_otto_worker(cmd):
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            killed += 1
        except Exception:
            pass
    if killed:
        print(f"[otto] swept {killed} orphan STT worker(s)", file=sys.stderr, flush=True)
    return killed


class OttoApp(rumps.App):
    def __init__(self) -> None:
        # Colored logo that changes with state; text fallback so it's never blank.
        self._has_icon = _icon("idle") is not None
        self._current_icon = "idle"
        if self._has_icon:
            super().__init__("Orrin", icon=_icon("idle"), template=False, quit_button=None)
        else:
            super().__init__("Orrin", title="Orrin", quit_button=None)
        from .paths import augment_path
        augment_path()   # so node/npx-based MCP tools (browser, computer-use) can be spawned
        self.settings = load_settings()
        self.bus = EventBus()
        self.orch = Orchestrator(self.settings, self.bus)
        # The menu bar owns global push-to-talk (installed on the MAIN thread via NSEvent),
        # so the orchestrator must NOT start the pynput listener (which SIGTRAPs the .app).
        self.orch._external_ptt = True
        self.url = f"http://{self.settings.server.host}:{self.settings.server.port}"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: uvicorn.Server | None = None
        self._dash = None       # in-app dashboard window (lazy)
        self._float = None      # floating on-screen character panel (lazy)
        self._char_shown = False   # whether we've auto-shown the character this launch
        self._shutting_down = False   # set on intentional Quit/Restart so the supervisor stops

        self.status_item = rumps.MenuItem("Starting…")
        self.project_menu = rumps.MenuItem("Project")
        self.workspace_menu = rumps.MenuItem("Workspaces")
        self.chat_menu = rumps.MenuItem("Chat")
        self.mic_item = rumps.MenuItem("Mute microphone", callback=self.toggle_mic)
        self.voice_item = rumps.MenuItem("Mute voice", callback=self.toggle_voice)
        self.dictation_item = rumps.MenuItem("Dictation mode", callback=self.toggle_dictation)
        self.watch_item = rumps.MenuItem("Watch mode (nap)", callback=self.toggle_watch)
        self.enroll_item = rumps.MenuItem("Enroll my voice", callback=self.enroll_voice)
        self.move_menu = rumps.MenuItem("Move Orrin")
        for _label, _c in (("Bottom-right", "br"), ("Bottom-left", "bl"),
                           ("Top-right", "tr"), ("Top-left", "tl")):
            self.move_menu.add(rumps.MenuItem(
                _label, callback=(lambda cc: (lambda _s: self._move_character(cc)))(_c)))
        self.menu = [
            self.status_item,
            None,
            rumps.MenuItem("🎤 Talk to Orrin", callback=self.talk),
            rumps.MenuItem("Show Orrin (floating)", callback=self.toggle_character),
            self.move_menu,
            self.watch_item,
            self.enroll_item,
            rumps.MenuItem("Open Dashboard", callback=self.open_dashboard),
            rumps.MenuItem("Settings…", callback=self.open_settings),
            self.project_menu,
            self.workspace_menu,
            self.chat_menu,
            self.mic_item,
            self.voice_item,
            self.dictation_item,
            None,
            rumps.MenuItem("Restart", callback=self.restart),
            rumps.MenuItem("Quit Orrin", callback=self.quit_app),
        ]
        self._proj_tick = 0
        self._mic_asked = False    # one-shot: request mic on the main thread once booted
        self._hotkey = None        # global push-to-talk monitor (installed on the main thread)
        self._hotkey_asked = False
        self._refresh_projects()
        self._refresh_workspaces()
        self._refresh_chats()

        threading.Thread(target=self._run_backend, daemon=True).start()

    # -- project switcher --------------------------------------------------
    def _refresh_projects(self) -> None:
        try:
            projs = list_projects(current=self.orch.current_project,
                                  pinned=self.settings.brain.projects)
        except Exception:
            return
        # rumps builds a submenu's backing NSMenu lazily on first .add(); only .clear()
        # once it exists (avoids AttributeError on the very first refresh).
        if getattr(self.project_menu, "_menu", None) is not None:
            try:
                self.project_menu.clear()
            except Exception:
                pass
        for p in projs:
            try:
                item = rumps.MenuItem(p.name, callback=self._make_switch(p.path))
                item.state = 1 if p.current else 0
                self.project_menu.add(item)
            except Exception:
                pass

    def _make_switch(self, path: str):
        def _cb(_):
            if self._loop is not None:
                self._loop.call_soon_threadsafe(
                    lambda: asyncio.ensure_future(self.orch.switch_project(path))
                )
        return _cb

    # -- parallel workspaces (focus one; each runs its own Claude) ---------
    def _refresh_workspaces(self) -> None:
        try:
            pool = list(getattr(self.orch, "workspaces", {}).values())
            focused = getattr(self.orch, "focused", None)
        except Exception:
            return
        if getattr(self.workspace_menu, "_menu", None) is not None:
            try:
                self.workspace_menu.clear()
            except Exception:
                pass
        # Only meaningful with more than one open; still show the single one for clarity.
        state_dot = {"idle": "○", "thinking": "●", "working": "●", "speaking": "●"}
        for ws in pool:
            try:
                dot = state_dot.get(ws.state, "○")
                item = rumps.MenuItem(f"{dot} {ws.name}",
                                      callback=self._make_focus_ws(ws.project))
                item.state = 1 if ws.project == focused else 0
                self.workspace_menu.add(item)
            except Exception:
                pass

    def _make_focus_ws(self, path: str):
        def _cb(_):
            if self._loop is not None:
                self._loop.call_soon_threadsafe(
                    lambda: self.orch.focus_workspace(path)
                )
        return _cb

    # -- chat (session) switcher ------------------------------------------
    def _refresh_chats(self) -> None:
        try:
            current = getattr(self.orch.brain, "session_id", None) or \
                getattr(self.orch, "_active_session", None)
            chats = list_sessions(self.orch.current_project, current_id=current, limit=12)
        except Exception:
            return
        if getattr(self.chat_menu, "_menu", None) is not None:
            try:
                self.chat_menu.clear()
            except Exception:
                pass
        try:
            self.chat_menu.add(rumps.MenuItem("＋ New chat", callback=self._new_chat_cb))
            self.chat_menu.add(rumps.separator)
        except Exception:
            pass
        for s in chats:
            try:
                item = rumps.MenuItem(s.title, callback=self._make_switch_chat(s.session_id))
                item.state = 1 if s.current else 0
                self.chat_menu.add(item)
            except Exception:
                pass

    def _make_switch_chat(self, session_id: str):
        def _cb(_):
            if self._loop is not None:
                self._loop.call_soon_threadsafe(
                    lambda: asyncio.ensure_future(self.orch.switch_session(session_id))
                )
        return _cb

    def _new_chat_cb(self, _):
        if self._loop is not None:
            self._loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(self.orch.new_chat())
            )

    # -- backend (its own asyncio loop, off the main thread) ---------------
    def _run_backend(self) -> None:
        """Supervise the backend: if the asyncio loop (orchestrator + brain + STT + server)
        crashes, restart it in-process instead of leaving a live menu bar over a dead backend
        (the silent-zombie failure). Crash-loop guard: if it keeps dying fast, exit the whole
        process so launchd (KeepAlive) can relaunch a clean one."""
        crashes: list[float] = []
        while not self._shutting_down:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            clean = False
            try:
                self._loop.run_until_complete(self._backend_main())
                clean = True                      # server exited normally (Quit/Restart)
            except Exception as e:                # backend crashed — log and recover
                import traceback
                self.orch.state = f"error: {e}"
                print(f"[otto] backend crashed: {e}", file=sys.stderr, flush=True)
                traceback.print_exc()
            finally:
                try:
                    self._loop.close()
                except Exception:
                    pass
            if clean or self._shutting_down:
                return
            now = time.monotonic()
            crashes = [t for t in crashes if now - t < 120.0] + [now]
            if len(crashes) >= 4:                 # crash-looping → a fresh process is cleaner
                # Kill our STT worker children FIRST — otherwise they reparent to launchd and the
                # NEXT launch collides with them on the mic (the recurring "not listening" flood).
                try:
                    _sweep_orphan_workers()
                except Exception:
                    pass
                print("[otto] backend crash-looping — exiting for a clean relaunch. If Orrin was "
                      "started from the source launcher (not launchd), relaunch it "
                      "(~/Launch Orrin.command).", file=sys.stderr, flush=True)
                os._exit(1)
            backoff = min(10.0, 2.0 ** len(crashes))
            print(f"[otto] restarting backend in {backoff:.0f}s…", file=sys.stderr, flush=True)
            self.orch.state = "restarting"
            time.sleep(backoff)
            # Clear any STT workers the crashed backend orphaned, then rebuild a fresh
            # orchestrator so we don't inherit half-dead state or colliding recorders.
            try:
                _sweep_orphan_workers()
            except Exception:
                pass
            try:
                self.orch = Orchestrator(self.settings, self.bus)
                self.orch._external_ptt = True
            except Exception as e:
                print(f"[otto] could not rebuild orchestrator: {e!r}", file=sys.stderr, flush=True)
                try:
                    _sweep_orphan_workers()
                except Exception:
                    pass
                os._exit(1)

    async def _backend_main(self) -> None:
        await self.orch.start()
        config = uvicorn.Config(
            create_app(self.orch, self.bus),
            host=self.settings.server.bind_host(),   # 0.0.0.0 when remote access is enabled
            port=self.settings.server.port,
            log_level="warning",
            loop="asyncio",
        )
        self._server = uvicorn.Server(config)
        # We're not on the main thread, so uvicorn can't install signal handlers.
        self._server.install_signal_handlers = lambda: None
        await self._server.serve()

    # -- menu (main thread) ------------------------------------------------
    @rumps.timer(1)
    def _tick(self, _) -> None:
        try:
            self.status_item.title = _STATE_LABELS.get(self.orch.state, self.orch.state)
            # Swap the colored logo to match the live state (grey idle, colors when active).
            name = "muted" if self.orch.mic_muted else _STATE_ICON.get(self.orch.state, "idle")
            if self._has_icon:
                if name != self._current_icon:
                    p = _icon(name)
                    if p:
                        self.icon = p
                        self._current_icon = name
                self.title = ""     # icon carries the state; no text needed
            else:
                self.title = "Orrin"
        except Exception:
            if not self._has_icon:
                self.title = "Orrin"
        # Auto-show the floating character once the server is up (if enabled). One-shot per launch;
        # the panel can still be toggled from the menu regardless of this default.
        if (not self._char_shown and self._server is not None
                and getattr(self.settings.voice, "floating_character", False)):
            self._char_shown = True
            self._show_character()
        # Reflect watch-mode (nap) state in the menu label.
        try:
            self.watch_item.title = ("Wake Orrin" if getattr(self.orch, "_sleeping", False)
                                     else "Watch mode (nap)")
        except Exception:
            pass
        # Request microphone access ONCE, on the main thread (this AppKit run loop) — the only
        # place a menu-bar agent's mic prompt actually renders and yields a real grant. The
        # recorder child (a re-exec of the signed binary) inherits the app's grant; it can't get
        # its own prompt. When the user answers, tell the orchestrator to bring the ears up.
        if not self._mic_asked and self._loop is not None:
            self._mic_asked = True
            self._ask_microphone()
        # Install the global push-to-talk hotkey ONCE, also on the main thread (NSEvent
        # monitors require it) — the same reason the mic prompt lives here.
        if not self._hotkey_asked and self._loop is not None:
            self._hotkey_asked = True
            self._install_hotkey()
        # Refresh the project list (and current checkmark) every ~8s.
        self._proj_tick = (self._proj_tick + 1) % 8
        if self._proj_tick == 0:
            self._refresh_projects()
            self._refresh_workspaces()
            self._refresh_chats()

    def _ask_microphone(self) -> None:
        """Fire the macOS mic prompt from the main thread; hand the result to the orchestrator."""
        if not self.settings.voice.input_enabled:
            return
        try:
            from .preflight import request_mic_access_async

            def _cb(granted: bool) -> None:
                if self._loop is not None:
                    self._loop.call_soon_threadsafe(
                        lambda: self.orch.on_mic_decision(granted))
            request_mic_access_async(_cb)
        except Exception as e:
            print(f"[otto] mic request error: {e}", file=sys.stderr, flush=True)

    def _install_hotkey(self) -> None:
        """Install the global push-to-talk monitor on the main thread (opt-in via config)."""
        if not self.settings.voice.hotkey.enabled:
            return
        try:
            # Register/prompt for Input Monitoring for THIS process on the main thread — a
            # background service is a separate TCC context, so it must ask for itself (a grant
            # given to some other process/terminal doesn't apply here).
            from .preflight import request_input_monitoring
            request_input_monitoring()
        except Exception as e:
            print(f"[otto] input-monitoring request error: {e}", file=sys.stderr, flush=True)
        try:
            from .voice.hotkey_mac import MacHotkey

            def press():
                if self._loop is not None:
                    self._loop.call_soon_threadsafe(self.orch.arm_ptt)

            def release():   # arm-next-utterance model: VAD ends the turn, nothing on release
                pass

            self._hotkey = MacHotkey(self.settings, self._loop, press, release)
            self._hotkey.start()
        except Exception as e:
            print(f"[otto] hotkey install error: {e}", file=sys.stderr, flush=True)

    def talk(self, _) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self.orch.arm_ptt)

    def open_dashboard(self, _) -> None:
        self._open_dash("")

    def open_settings(self, _) -> None:
        # Open the dashboard's inline settings drawer (in-app, or browser fallback).
        self._open_dash("#settings")

    def _open_dash(self, suffix: str) -> None:
        """Show the dashboard in a native in-app window (WKWebView); fall back to the browser."""
        try:
            from .webview import DashboardWindow, available
            if available():
                if self._dash is None:
                    self._dash = DashboardWindow(self.url, title="Orrin")
                self._dash.show(suffix)
                return
        except Exception as e:
            print(f"[otto] in-app dashboard unavailable, using browser: {e}",
                  file=sys.stderr, flush=True)
        webbrowser.open(self.url + ("/" + suffix if suffix else ""))

    def toggle_character(self, _) -> None:
        self._show_character(toggle=True)

    def _move_character(self, corner: str) -> None:
        self._show_character()
        if self._float is not None:
            try:
                self._float.move_to(corner)
            except Exception as e:
                print(f"[otto] move character failed: {e}", file=sys.stderr, flush=True)

    def _show_character(self, toggle: bool = False) -> None:
        """Show/hide the floating on-screen Orrin character panel (WKWebView)."""
        try:
            from .webview import FloatingCharacter, available
            if not available():
                return
            if self._float is None:
                self._float = FloatingCharacter(f"{self.url}/hud")
            if toggle:
                self._float.toggle()
            else:
                self._float.show()
            print(f"[otto] floating character shown (visible={self._float.visible})",
                  file=sys.stderr, flush=True)
        except Exception as e:
            print(f"[otto] floating character unavailable: {e}", file=sys.stderr, flush=True)

    def enroll_voice(self, _) -> None:
        """Kick off in-app voice enrollment (speak a few sentences; Orrin builds your voiceprint)."""
        try:
            if self.orch._loop is not None:
                self.orch._loop.call_soon_threadsafe(self.orch.start_enrollment)
        except Exception as e:
            print(f"[otto] enroll trigger failed: {e}", file=sys.stderr, flush=True)

    def toggle_watch(self, _) -> None:
        """Watch mode: nap (ignore everything but the wake word) so a match/movie/call doesn't
        trigger Orrin — and the character visibly sleeps. Toggle back to wake it."""
        try:
            if getattr(self.orch, "_sleeping", False):
                self.orch.wake_now()
            else:
                self.orch.sleep_now()
        except Exception as e:
            print(f"[otto] watch-mode toggle failed: {e}", file=sys.stderr, flush=True)

    def toggle_mic(self, _) -> None:
        self.orch.mic_muted = not self.orch.mic_muted
        self.mic_item.title = "Unmute microphone" if self.orch.mic_muted else "Mute microphone"

    def toggle_voice(self, _) -> None:
        self.orch.voice_muted = not self.orch.voice_muted
        self.voice_item.title = "Unmute voice" if self.orch.voice_muted else "Mute voice"

    def toggle_dictation(self, _) -> None:
        if self._loop is not None:
            want = not self.orch._dictation
            self._loop.call_soon_threadsafe(lambda: self.orch.set_dictation(want))
            self.dictation_item.title = "Dictation mode ✓" if want else "Dictation mode"

    def restart(self, _) -> None:
        # Exit cleanly; launchd (KeepAlive) relaunches a fresh process that rebuilds the
        # menu bar. os.execv re-execs in place and leaves the menu bar item blank.
        self._shutting_down = True
        if self._server is not None:
            self._server.should_exit = True
        os._exit(0)

    def quit_app(self, _) -> None:
        # Stop the backend, then actually STOP — not just exit. When Otto runs under launchd
        # (the installed menu-bar app), the LaunchAgent has KeepAlive=true, so a plain exit is
        # immediately relaunched. To truly quit we must unload the job with `launchctl bootout`;
        # that also terminates this process. In a dev run (no launchd job) bootout is a no-op and
        # we fall back to a normal quit.
        self._shutting_down = True
        if self._server is not None:
            self._server.should_exit = True
        if self._loop is not None:
            self._loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(self.orch.stop())
            )
        try:
            r = subprocess.run(
                ["launchctl", "bootout", f"gui/{os.getuid()}/{_LAUNCHD_LABEL}"],
                capture_output=True, timeout=5,
            )
            if r.returncode == 0:      # job unloaded; won't relaunch — make sure we're gone
                os._exit(0)
        except Exception:
            pass
        rumps.quit_application()


def main() -> None:
    load_env()
    # Refuse to run two Orrins at once (the recurring "stopped listening" mic collision), and
    # clear any STT workers a prior crash left holding the mic.
    _acquire_single_instance_lock_or_exit()
    _sweep_orphan_workers()
    print("[otto] menu bar app starting…", file=sys.stderr, flush=True)
    OttoApp().run()


if __name__ == "__main__":
    main()
