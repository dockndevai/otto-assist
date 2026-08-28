"""Otto as a macOS menu bar app.

The AppKit run loop needs the main thread, so rumps runs there while the assistant
(orchestrator + brain + dashboard server) runs on its own asyncio loop in a background
thread. Menu actions on the main thread poke that loop via thread-safe calls; a 1s timer
reads the orchestrator's live state to keep the menu label current.

Run:  python -m otto.menubar
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
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
from .server.app import create_app
from .server.events import EventBus

_STATE_LABELS = {
    "starting": "Starting…",
    "idle": "● Ready",
    "listening": "● Listening…",
    "thinking": "● Thinking…",
    "speaking": "● Speaking…",
    "working": "● Working…",
}


_ASSETS = os.path.join(os.path.dirname(__file__), "assets")


def _icon(name: str) -> str | None:
    p = os.path.join(_ASSETS, f"mb_{name}.png")
    return p if os.path.exists(p) else None


# Which colored robot to show for each live state (grey idle, colors when active).
_STATE_ICON = {
    "starting": "idle", "idle": "idle", "listening": "listening",
    "thinking": "thinking", "working": "thinking", "speaking": "speaking",
}


class OttoApp(rumps.App):
    def __init__(self) -> None:
        # Colored logo that changes with state; text fallback so it's never blank.
        self._has_icon = _icon("idle") is not None
        self._current_icon = "idle"
        if self._has_icon:
            super().__init__("Otto", icon=_icon("idle"), template=False, quit_button=None)
        else:
            super().__init__("Otto", title="Otto", quit_button=None)
        self.settings = load_settings()
        self.bus = EventBus()
        self.orch = Orchestrator(self.settings, self.bus)
        self.url = f"http://{self.settings.server.host}:{self.settings.server.port}"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: uvicorn.Server | None = None

        self.status_item = rumps.MenuItem("Starting…")
        self.project_menu = rumps.MenuItem("Project")
        self.mic_item = rumps.MenuItem("Mute microphone", callback=self.toggle_mic)
        self.voice_item = rumps.MenuItem("Mute voice", callback=self.toggle_voice)
        self.menu = [
            self.status_item,
            None,
            rumps.MenuItem("🎤 Talk to Otto", callback=self.talk),
            rumps.MenuItem("Open Dashboard", callback=self.open_dashboard),
            rumps.MenuItem("Settings…", callback=self.open_settings),
            self.project_menu,
            self.mic_item,
            self.voice_item,
            None,
            rumps.MenuItem("Restart", callback=self.restart),
            rumps.MenuItem("Quit Otto", callback=self.quit_app),
        ]
        self._proj_tick = 0
        self._refresh_projects()

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

    # -- backend (its own asyncio loop, off the main thread) ---------------
    def _run_backend(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._backend_main())
        except Exception as e:  # surface fatal backend errors to the menu + log
            import traceback

            self.orch.state = f"error: {e}"
            print(f"[otto] backend crashed: {e}", file=sys.stderr, flush=True)
            traceback.print_exc()

    async def _backend_main(self) -> None:
        await self.orch.start()
        config = uvicorn.Config(
            create_app(self.orch, self.bus),
            host=self.settings.server.host,
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
                self.title = "Otto"
        except Exception:
            if not self._has_icon:
                self.title = "Otto"
        # Refresh the project list (and current checkmark) every ~8s.
        self._proj_tick = (self._proj_tick + 1) % 8
        if self._proj_tick == 0:
            self._refresh_projects()

    def talk(self, _) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self.orch.arm_ptt)

    def open_dashboard(self, _) -> None:
        webbrowser.open(self.url)

    def open_settings(self, _) -> None:
        # Open the dashboard's inline settings drawer (falls back to the standalone page too).
        webbrowser.open(self.url + "/#settings")

    def toggle_mic(self, _) -> None:
        self.orch.mic_muted = not self.orch.mic_muted
        self.mic_item.title = "Unmute microphone" if self.orch.mic_muted else "Mute microphone"

    def toggle_voice(self, _) -> None:
        self.orch.voice_muted = not self.orch.voice_muted
        self.voice_item.title = "Unmute voice" if self.orch.voice_muted else "Mute voice"

    def restart(self, _) -> None:
        # Exit cleanly; launchd (KeepAlive) relaunches a fresh process that rebuilds the
        # menu bar. os.execv re-execs in place and leaves the menu bar item blank.
        if self._server is not None:
            self._server.should_exit = True
        os._exit(0)

    def quit_app(self, _) -> None:
        # Stop the backend, then actually STOP — not just exit. When Otto runs under launchd
        # (the installed menu-bar app), the LaunchAgent has KeepAlive=true, so a plain exit is
        # immediately relaunched. To truly quit we must unload the job with `launchctl bootout`;
        # that also terminates this process. In a dev run (no launchd job) bootout is a no-op and
        # we fall back to a normal quit.
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
    print("[otto] menu bar app starting…", file=sys.stderr, flush=True)
    OttoApp().run()


if __name__ == "__main__":
    main()
