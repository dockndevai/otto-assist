"""The orchestrator: the realtime loop tying ears -> brain -> mouth together, and the
bridge the agent's tools call back into (say / ask_user / render_canvas / confirm).

State machine per turn:  idle -> (listening) -> thinking -> speaking -> idle
Barge-in: any speech-start while speaking stops the mouth immediately.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import subprocess
import sys
from typing import Any


from . import sessions
from .brain.base import Brain
from .config import OTTO_HOME, Settings, save_config
from .history import History
from .server.events import EventBus
from .workspace import Workspace

_log = logging.getLogger("otto")

_SENTENCE_END = re.compile(r"[^.!?\n]*[.!?\n]+")

# Common speech-to-text mishearings of the "otto" trigger word.
_WAKE_ALIASES = {
    "otto": {"otto", "auto", "oto", "ottos", "otho", "atto", "alto", "otter",
             "ottoh", "audo", "auddo", "ado", "ottto", "hotto", "otta", "auddo"},
}
_GREETINGS = {"hey", "hi", "ok", "okay", "yo", "hello", "hay"}

# Spoken when you say the wake word alone — warm and varied, not a robotic "yes".
_WAKE_ACKS = [
    "Hey! What can I do for you?",
    "Yes? I'm listening.",
    "Hi there — what do you need?",
    "Hey, I'm here. What's up?",
    "Sure — what would you like?",
    "Go ahead, I'm listening.",
    "Hey! How can I help?",
    "Yep — what can I do?",
]


def _norm_words(s: str) -> list[str]:
    """Lowercase, split hyphens/slashes into words, drop other punctuation."""
    s = re.sub(r"[-_/]+", " ", s.lower())
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return s.split()


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _wordish(word: str, target: str) -> bool:
    """Fuzzy word match tolerant of STT mishearings (Otto/Auto/…)."""
    if word == target:
        return True
    if word in _WAKE_ALIASES.get(target, set()):
        return True
    # small edit distance for words long enough to be distinctive
    if len(target) >= 4 and _levenshtein(word, target) <= 2:
        return True
    return False

_DEMO_CHART = """<!doctype html><meta charset="utf-8">
<style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;margin:0;padding:34px;color:#e6ecf6;
background:radial-gradient(700px 400px at 20% 0%,rgba(34,211,238,.10),transparent),#0a0f1c}
h2{margin:0 0 4px;font-size:22px}.sub{color:#7d8aa3;font-size:13px;margin-bottom:24px;letter-spacing:1px}
.bar{height:30px;border-radius:7px;background:linear-gradient(90deg,#22d3ee,#a855f7);line-height:30px;
box-shadow:0 0 16px rgba(34,211,238,.25)}
.row{display:grid;grid-template-columns:120px 1fr 60px;align-items:center;gap:12px;margin:12px 0}
.lbl{font-size:14px;font-weight:600}.val{font-size:13px;color:#9fb0c8;text-align:right}</style>
<h2>Global EV Market Share — 2024</h2><div class="sub">sample data · rendered via render_canvas</div>
<div class="row"><div class="lbl">BYD</div><div class="bar" style="width:100%"></div><div class="val">22.3%</div></div>
<div class="row"><div class="lbl">Tesla</div><div class="bar" style="width:89%"></div><div class="val">19.9%</div></div>
<div class="row"><div class="lbl">VW Group</div><div class="bar" style="width:47%"></div><div class="val">10.4%</div></div>
<div class="row"><div class="lbl">GM</div><div class="bar" style="width:34%"></div><div class="val">7.6%</div></div>
<div class="row"><div class="lbl">Hyundai-Kia</div><div class="bar" style="width:32%"></div><div class="val">7.1%</div></div>"""
_AFFIRMATIVE = {"yes", "yeah", "yep", "sure", "ok", "okay", "go", "go ahead",
                "do it", "please do", "confirm", "affirmative", "yup"}


class Orchestrator:
    def __init__(self, settings: Settings, bus: EventBus) -> None:
        self.settings = settings
        self.bus = bus
        self.history = History(OTTO_HOME / "history.jsonl")
        bus.add_sink(self.history.sink)   # persist transcript + canvas as they publish
        # Parallel workspaces: a bounded pool of projects, each running its own Claude
        # (its own subprocess), one of them focused. Only the focused workspace streams to
        # the screen and speaks; background ones run headless and notify when they finish.
        self.max_workspaces = 3
        self.focused: str = str(settings.project_path)
        self.workspaces: dict[str, Workspace] = {
            self.focused: Workspace(self, settings, self.focused,
                                    resume=settings.brain.sessions.get(self.focused)),
        }
        self.mouth = None       # set in start() if voice output enabled
        self.ears = None        # set in start() if voice input enabled
        self._ptt = None
        self._eff = None        # EfficientWord-Net custom wake-word listener
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending_answer: asyncio.Future[str] | None = None
        self._speech_buf = ""
        self._speaking = False
        # Live state + controls (read/driven by the menu bar app).
        self.state = "starting"
        self.mic_muted = False
        self.voice_muted = False
        # Software ("phrase") wake word: await the command after "hey otto".
        self._awaiting_command = False
        self._await_handle = None
        self._last_ack = ""     # last greeting spoken (to ignore its own echo)
        # Push-to-talk: capture the next utterance as a command, no wake word needed.
        self._ptt_armed = False
        self._ptt_handle = None

    # -- workspace pool accessors -----------------------------------------
    # Most existing code operates on the *focused* workspace; these keep that code
    # (and the REST/menu-bar callers) working unchanged while the pool holds several.
    @property
    def focused_ws(self) -> Workspace:
        return self.workspaces[self.focused]

    @property
    def brain(self) -> Brain:
        return self.focused_ws.brain

    @property
    def current_project(self) -> str:
        return self.focused

    @property
    def _active_session(self) -> str | None:
        return self.focused_ws.session_id

    def _status(self, state: str, text: str = "") -> None:
        self.state = state
        self.bus.status(state, text)

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self.brain.connect()

        v = self.settings.voice
        if v.output_enabled:
            from .voice.tts import Mouth
            self.mouth = Mouth(self.settings)
            try:
                self.mouth.start()
            except Exception as e:  # missing extras / audio device
                self.mouth = None
                self.bus.notify("Voice output off", str(e))

        if v.input_enabled:
            try:
                from .voice.stt import Ears
                self.ears = Ears(
                    self.settings, self._loop,
                    on_final=self._on_voice_final,
                    on_partial=lambda t: self.bus.transcript("user", t, final=False),
                    on_speech_start=self._on_speech_start,
                    on_wake=self._on_speech_start,
                    on_recording_stop=self._on_recording_stop,
                )
                self.ears.start()
                # Global push-to-talk hotkey is opt-in: pynput's macOS input-source calls run
                # off the main thread and hard-abort (SIGTRAP) in the packaged app, and it needs
                # Accessibility permission anyway. The dashboard mic button + wake word cover PTT.
                if self.settings.voice.hotkey.enabled:
                    from .voice.hotkey import PushToTalk
                    self._ptt = PushToTalk(
                        self.settings, self._loop,
                        on_press=self._on_ptt_press,
                        on_release=self._on_ptt_release,
                    )
                    self._ptt.start()
            except Exception as e:
                self.ears = None
                self.bus.notify("Voice input off", str(e))

            # Custom few-shot wake word (EfficientWord-Net): a separate acoustic listener
            # that arms capture on detection — reliable, and any phrase the user enrolled.
            stt = v.stt
            if stt.wakeword_backend == "efficientword" and stt.eff_reference:
                try:
                    from .voice.wakeword_eff import EffWakeWord
                    slug = "".join(c if c.isalnum() else "_"
                                   for c in stt.wake_words.lower()).strip("_")
                    self._eff = EffWakeWord(
                        reference_file=stt.eff_reference, hotword=slug,
                        threshold=stt.eff_threshold, loop=self._loop,
                        on_wake=self._on_eff_wake,
                    )
                    self._eff.start()
                except Exception as e:
                    self._eff = None
                    self.bus.notify("Wake word off", f"{type(e).__name__}: {e}")

        self._publish_project()
        self._publish_session()
        self._status("idle", "Ready")

    def _on_eff_wake(self) -> None:
        # Echo guard: the wake listener also hears Otto's own voice through the speaker —
        # ignore detections while Otto is speaking (or just spoke) to avoid a feedback loop.
        if self.mouth is not None and self.mouth.recently_active():
            return
        if self._awaiting_command:
            return   # already greeted and waiting; don't greet again
        print("[otto] wake word detected", file=sys.stderr, flush=True)
        self._enter_awaiting_command()   # greet warmly, then capture the command

    async def stop(self) -> None:
        if self.ears:
            self.ears.stop()
        if self._ptt:
            self._ptt.stop()
        if self._eff:
            self._eff.stop()
        for ws in list(self.workspaces.values()):
            await ws.brain.disconnect()

    def _publish_project(self) -> None:
        self.bus.publish({"type": "project", "path": self.current_project,
                          "name": self.current_project.rstrip("/").split("/")[-1]})
        self._publish_workspaces()

    def _publish_workspaces(self) -> None:
        """Broadcast the live pool so the dashboard can show every workspace + its state."""
        items = [{"project": ws.project, "name": ws.name, "state": ws.state,
                  "focused": ws.project == self.focused,
                  "session_id": ws.session_id}
                 for ws in self.workspaces.values()]
        self.bus.publish({"type": "workspaces", "items": items,
                          "focused": self.focused, "max": self.max_workspaces})

    # -- workspace pool (parallel projects) --------------------------------
    async def open_workspace(self, path: str, *, focus: bool = True) -> Workspace | None:
        """Open a project as a live workspace (its own Claude), optionally focusing it.

        Keeps any already-open workspaces running. Capped at ``max_workspaces``.
        """
        from pathlib import Path
        p = str(Path(path).expanduser())
        if p in self.workspaces:
            if focus:
                self.focus_workspace(p)
            return self.workspaces[p]
        if len(self.workspaces) >= self.max_workspaces:
            await self.say(f"I can run up to {self.max_workspaces} workspaces at once. "
                           "Close one first.")
            return None
        self._status("thinking", f"Opening {Path(p).name}…")
        ws = Workspace(self, self.settings, p,
                       resume=self.settings.brain.sessions.get(p))
        await ws.brain.connect()
        self.workspaces[p] = ws
        if focus:
            self.focus_workspace(p)
        else:
            self._publish_workspaces()
            self._status("idle", "Ready")
        return ws

    def focus_workspace(self, path: str) -> None:
        """Make an open workspace the focused one — it now streams to the screen + voice."""
        from pathlib import Path
        if path not in self.workspaces:
            return
        self.focused = path
        try:
            self.settings.brain.project_dir = path
            self._persist_project(path)
        except Exception:
            pass
        self._publish_project()
        self._publish_session()
        # Re-show whatever this workspace last drew, so the canvas reflects the focus.
        ws = self.workspaces[path]
        if ws.last_canvas:
            self.bus.canvas(ws.last_canvas, "html", ws=path)
        self._status("idle", f"Ready · {Path(path).name}")

    async def close_workspace(self, path: str) -> None:
        """Shut down a workspace's Claude and drop it from the pool."""
        ws = self.workspaces.pop(path, None)
        if ws is None:
            return
        try:
            await ws.brain.disconnect()
        except Exception:
            pass
        if self.focused == path:
            # Focus whatever remains (or reopen the default project if the pool emptied).
            if self.workspaces:
                self.focus_workspace(next(iter(self.workspaces)))
            else:
                await self.open_workspace(str(self.settings.project_path), focus=True)
        else:
            self._publish_workspaces()

    # Base directory for repos Otto clones itself (override with OTTO_WORKSPACES_DIR).
    def _workspaces_dir(self):
        from pathlib import Path
        return Path(os.environ.get("OTTO_WORKSPACES_DIR",
                                   str(Path.home() / "otto-workspaces"))).expanduser()

    async def clone_workspace(self, url: str, branch: str | None = None) -> Workspace | None:
        """Clone a git repo into a fresh directory and open it as a focused workspace —
        Otto making its own code space to work in. Reuses an existing clone of the same URL."""
        from pathlib import Path
        url = (url or "").strip().rstrip("/")
        if not re.match(r"^(https?://|git@)", url):
            await self.say("I can only clone http(s) or git URLs.")
            return None
        name = re.sub(r"\.git$", "", url.split("/")[-1]) or "repo"
        base = self._workspaces_dir()
        target = base / name
        tstr = str(target)
        if tstr in self.workspaces:            # already open — just focus it
            self.focus_workspace(tstr)
            return self.workspaces[tstr]
        if len(self.workspaces) >= self.max_workspaces:
            await self.say(f"I can run up to {self.max_workspaces} workspaces at once. "
                           "Close one first.")
            return None
        if (target / ".git").exists():         # already cloned before — reuse it
            await self.say(f"{name} is already cloned — opening it.")
            return await self.open_workspace(tstr, focus=True)
        self._status("thinking", f"Cloning {name}…")
        await self.say(f"Cloning {name}…")
        base.mkdir(parents=True, exist_ok=True)
        args = ["git", "clone", "--depth", "1"]
        if branch:
            args += ["--branch", branch]
        args += ["--", url, tstr]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await proc.communicate()
            if proc.returncode != 0:
                msg = (out or b"").decode(errors="replace").strip().splitlines()[-1:] or ["git clone failed"]
                self.notify("Clone failed", msg[0][:160])
                await self.say(f"Cloning {name} failed. {msg[0][:120]}")
                self._status("idle", "Ready")
                return None
        except Exception as e:
            await self.say(f"Cloning {name} failed: {e}")
            self._status("idle", "Ready")
            return None
        ws = await self.open_workspace(tstr, focus=True)
        if ws is not None:
            await self.say(f"{name} is ready — I'm in it now.")
        return ws

    async def switch_project(self, path: str) -> None:
        """Focus another project, keeping any parallel workspaces alive. Unlike opening a
        parallel workspace, *switching* always succeeds: at the cap it swaps out the
        currently-focused workspace rather than refusing."""
        from pathlib import Path
        p = str(Path(path).expanduser())
        if p not in self.workspaces and len(self.workspaces) >= self.max_workspaces:
            await self.close_workspace(self.focused)
        await self.open_workspace(p, focus=True)

    def _persist_project(self, path: str) -> None:
        import yaml
        from .config import config_path
        cfg_file = config_path()
        data = yaml.safe_load(cfg_file.read_text()) if cfg_file.exists() else {}
        data = data or {}
        data.setdefault("brain", {})["project_dir"] = path
        save_config(data)

    # -- chats (Claude sessions within the current project) ----------------
    def _current_chat_title(self) -> str:
        """Human label for the active chat (or 'New chat' before its first turn)."""
        sid = getattr(self.brain, "session_id", None) or self._active_session
        if not sid:
            return "New chat"
        for s in sessions.list_sessions(self.current_project, current_id=sid):
            if s.session_id == sid:
                return s.title
        return sid[:8]

    def _publish_session(self) -> None:
        sid = getattr(self.brain, "session_id", None) or self._active_session
        self.bus.publish({"type": "session", "session_id": sid,
                          "title": self._current_chat_title()})

    def _persist_session(self, project: str, session_id: str) -> None:
        import yaml
        from .config import config_path
        cfg_file = config_path()
        data = yaml.safe_load(cfg_file.read_text()) if cfg_file.exists() else {}
        data = data or {}
        data.setdefault("brain", {}).setdefault("sessions", {})[project] = session_id
        save_config(data)

    async def switch_session(self, session_id: str) -> None:
        """Re-bind the focused workspace to a specific chat (session) in its project."""
        session_id = (session_id or "").strip()
        if not session_id or session_id == (self._active_session or ""):
            return
        ws = self.focused_ws
        self._status("thinking", "Switching chat…")
        async with ws.lock:                 # don't swap the brain mid-turn (or let a turn
            await ws.rebind(self.settings, resume=session_id)   # race the swap)
        try:
            self._persist_session(ws.project, session_id)
        except Exception:
            pass
        self._publish_session()
        self._status("idle", "Ready")
        await self.say("Switched chats.")

    async def new_chat(self) -> None:
        """Start a fresh chat in the focused workspace's project (a new Claude session)."""
        ws = self.focused_ws
        self._status("thinking", "Starting a new chat…")
        async with ws.lock:                 # serialize with turns so a command fired right
            await ws.rebind(self.settings, new_chat=True)   # after lands on the NEW chat
        # The id isn't minted until the first turn completes; _run_turn will persist it.
        self._publish_session()
        self._status("idle", "Ready")
        await self.say("Started a new chat.")

    async def rename_chat(self, title: str) -> None:
        """Rename the active chat. Needs at least one completed turn (an id to rename)."""
        title = (title or "").strip()
        if not title:
            return
        sid = getattr(self.brain, "session_id", None) or self._active_session
        if not sid:
            await self.say("Send a message first, then I can name this chat.")
            return
        try:
            sessions.rename_session_title(self.current_project, sid, title)
        except Exception as e:
            self.bus.notify("Rename failed", str(e))
            return
        self._publish_session()
        await self.say(f"Renamed the chat to {title}.")

    # Voice/text chat commands, parsed BEFORE the brain turn (switching tears down the
    # very brain that would answer, so it must be handled at the orchestrator level).
    _RE_NEW_CHAT = re.compile(r"^(?:start|open|create|begin)\s+(?:a\s+)?new\s+chat\.?$", re.I)
    _RE_SWITCH_CHAT = re.compile(
        r"^switch\s+(?:to\s+)?(?:the\s+)?(?P<name>.+?)\s+chat\.?$", re.I)
    _RE_RENAME_CHAT = re.compile(
        r"^rename\s+(?:this\s+|the\s+|current\s+)?chat\s+to\s+(?P<title>.+?)\.?$", re.I)
    # "in <name>, <command>" / "in the <name> workspace: <command>" — target another workspace.
    _RE_TARGET = re.compile(
        r"^in\s+(?:the\s+)?(?P<name>.+?)(?:\s+workspace)?\s*[,:]\s*(?P<cmd>.+)$", re.I)
    # "clone <url> [branch <b>]" / "set up a workspace from <url>" — Otto makes its own code space.
    _RE_CLONE = re.compile(
        r"^(?:clone|check\s?out|set\s*up|open|create)\s+(?:a\s+)?(?:new\s+)?(?:workspace\s+"
        r"(?:from|for)\s+|repo(?:sitory)?\s+|the\s+repo(?:sitory)?\s+)?"
        r"(?P<url>(?:https?://|git@)\S+?)(?:\.git)?/?"
        r"(?:\s+(?:on\s+|from\s+|using\s+)?branch\s+(?P<branch>[\w./-]+))?\.?$", re.I)

    def _maybe_clone_command(self, text: str) -> bool:
        """If `text` asks to clone a repo into a new workspace, do it. Returns True if consumed."""
        m = self._RE_CLONE.match(text.strip())
        if not m:
            return False
        assert self._loop is not None
        self.bus.transcript("user", text, final=True)
        self._loop.create_task(self.clone_workspace(m.group("url"), m.group("branch")))
        return True

    def _maybe_chat_command(self, text: str) -> bool:
        """If `text` is a chat command, act on it and return True (input consumed)."""
        assert self._loop is not None
        if self._RE_NEW_CHAT.match(text):
            self.bus.transcript("user", text, final=True)
            self._loop.create_task(self.new_chat())
            return True
        m = self._RE_RENAME_CHAT.match(text)
        if m:
            self.bus.transcript("user", text, final=True)
            self._loop.create_task(self.rename_chat(m.group("title").strip()))
            return True
        m = self._RE_SWITCH_CHAT.match(text)
        if m:
            self.bus.transcript("user", text, final=True)
            self._loop.create_task(self._switch_chat_by_name(m.group("name").strip()))
            return True
        return False

    async def _switch_chat_by_name(self, name: str) -> None:
        """Resolve a spoken chat name to a session and switch, or ask to disambiguate."""
        needle = name.lower().strip()
        chats = sessions.list_sessions(self.current_project,
                                       current_id=self._active_session)
        matches = [s for s in chats if needle in s.title.lower()]
        if not matches:
            await self.say(f"I couldn't find a chat matching “{name}”.")
            return
        if len(matches) > 1:
            names = ", ".join(s.title for s in matches[:4])
            await self.say(f"I found a few chats matching that: {names}. Which one?")
            return
        await self.switch_session(matches[0].session_id)

    def _resolve_workspace_name(self, name: str) -> str | None:
        """Map a spoken workspace name to a project path: an open workspace first, then a
        known project. Returns the project path, or None if nothing matches."""
        needle = name.lower().strip()
        for ws in self.workspaces.values():
            if needle in ws.name.lower():
                return ws.project
        try:
            from .projects import list_projects
            for p in list_projects(current=self.focused,
                                   pinned=self.settings.brain.projects):
                if needle in p.name.lower():
                    return p.path
        except Exception:
            pass
        return None

    async def _targeted_turn(self, name: str, cmd: str, original: str) -> None:
        """Run a command in the named workspace (opening it if needed), in the background.

        If the name doesn't resolve to a workspace, this wasn't really targeting — treat the
        whole utterance as a normal command in the focused workspace.
        """
        path = self._resolve_workspace_name(name)
        if not path:
            await self._run_turn(self.focused_ws, original)
            return
        ws = self.workspaces.get(path)
        if ws is None:
            ws = await self.open_workspace(path, focus=False)
            if ws is None:
                return   # pool full — open_workspace already explained
        if ws.project == self.focused:
            await self._run_turn(ws, cmd)          # _run_turn echoes the focused command
        else:
            self.bus.transcript("user", original, final=True)   # show it before it runs quietly
            await self._run_turn(ws, cmd)

    # -- input routing -----------------------------------------------------
    def submit_user_input(self, text: str) -> None:
        """Entry point for both typed and spoken input."""
        text = (text or "").strip()
        if not text:
            return
        # If the agent is waiting on an ask_user answer, this input is that answer.
        if self._pending_answer and not self._pending_answer.done():
            self.bus.transcript("user", text, final=True)
            self._pending_answer.set_result(text)
            return
        # Built-in self-test: renders a sample chart end-to-end without the model.
        if text.strip().lower() == "/demo":
            self.bus.transcript("user", text, final=True)
            self._run_demo()
            return
        # Chat control ("switch to X chat", "start a new chat", "rename this chat to X").
        # Handled here — before the brain turn — because switching rebuilds the brain.
        if self._maybe_chat_command(text):
            return
        # "clone <url>" — Otto sets up a new code space (clone → open as a workspace).
        if self._maybe_clone_command(text):
            return
        assert self._loop is not None
        # Workspace targeting: "in <name>, <command>" runs the command in another workspace
        # (opening it if needed), concurrently, in the background. Only the focused workspace
        # streams to the screen + voice; a background one works silently and notifies on done.
        m = self._RE_TARGET.match(text)
        if m:
            self._loop.create_task(self._targeted_turn(
                m.group("name").strip(), m.group("cmd").strip(), text))
            return
        self._loop.create_task(self._handle_turn(text))

    def _run_demo(self) -> None:
        self.bus.tool_use("subagent", "researcher: sample dataset")
        self.bus.transcript(
            "assistant",
            "Here's a sample chart on the canvas — the dashboard is working.",
            final=True,
        )
        self.render_canvas(_DEMO_CHART, "html")
        self._status("idle", "Ready")

    def arm_ptt(self) -> None:
        """Push-to-talk: capture the NEXT spoken utterance as a command (skip wake word)."""
        self._ptt_armed = True
        self._status("listening", "Listening… speak now")
        if self.mouth:
            self.mouth.stop()
            self.mouth._last_end = 0.0   # allow immediate capture (bypass echo guard)
        if self._loop is not None:
            if self._ptt_handle is not None:
                self._ptt_handle.cancel()
            self._ptt_handle = self._loop.call_later(12, self._disarm_ptt)

    def _disarm_ptt(self) -> None:
        if self._ptt_armed:
            self._ptt_armed = False
            self._status("idle", "Ready")

    def _on_voice_final(self, text: str) -> None:
        if self.mic_muted:
            return
        # Push-to-talk armed: this utterance is the command, no wake word required.
        if self._ptt_armed:
            self._ptt_armed = False
            if self._ptt_handle is not None:
                self._ptt_handle.cancel()
            print(f"[otto] ptt heard: {text!r}", file=sys.stderr, flush=True)
            self.submit_user_input(text)
            return
        # Awaiting the command after a bare wake word (any backend). Ignore Otto's own
        # greeting echo and a repeated wake word; the next real utterance is the command.
        if self._awaiting_command:
            if self._is_ack_echo(text):
                print(f"[otto] ignoring greeting echo: {text!r}", file=sys.stderr, flush=True)
                return
            matched, remainder = self._match_wake(text)
            if matched and not remainder.strip():
                print(f"[otto] ignoring wake echo: {text!r}", file=sys.stderr, flush=True)
                return   # just the wake word again — keep waiting for the command
            command = remainder if (matched and remainder.strip()) else text
            self._cancel_await()
            print(f"[otto] command after wake: {command!r}", file=sys.stderr, flush=True)
            self.submit_user_input(command)
            return
        # Echo guard: while Otto is speaking (and for the transcription lag after), the mic
        # hears Otto's own voice — ignore it so it doesn't answer itself in a loop.
        if self.mouth is not None and self.mouth.recently_active(self._echo_window()):
            print(f"[otto] ignoring self-echo: {text!r}", file=sys.stderr, flush=True)
            return
        # Always-listen: no wake word, just respond to whatever you say (self-echo already
        # filtered above). Mute the mic when you don't want Otto listening.
        if self.settings.voice.stt.always_listen:
            print(f"[otto] always-listen: {text!r}", file=sys.stderr, flush=True)
            self.submit_user_input(text)
            return
        # "phrase" backend = software wake word: act on speech that begins with the wake
        # phrase (e.g. "hey otto"), or greet + await the command when it's said alone.
        if self.settings.voice.stt.wakeword_backend == "phrase":
            matched, remainder = self._match_wake(text)
            print(f"[otto] heard: {text!r} | matched={matched} cmd={remainder!r}",
                  file=sys.stderr, flush=True)
            if not matched:
                if self.state == "listening":
                    self._status("idle", "Ready")     # ignore ambient speech
                return
            if remainder.strip():
                self.submit_user_input(remainder)     # "hey otto, what time is it"
            else:
                self._enter_awaiting_command()        # just "hey otto" — wait for command
            return
        if self.settings.voice.stt.wakeword_backend == "efficientword":
            return   # wake handled acoustically by EffWakeWord -> greet + await; ignore rest
        self.submit_user_input(text)

    def _match_wake(self, text: str) -> tuple[bool, str]:
        """Does `text` start with the wake phrase (fuzzily)? -> (matched, command).

        Speech-to-text mishears the trigger a lot ("Otto"->"Auto", "Hey Otto"->"Hiyoto",
        "Otto open"->"auto-open"), so we normalize punctuation to spaces and match the
        trigger word — or the whole fused phrase — by aliases / small edit distance.
        """
        norm = _norm_words(text)
        phrase = _norm_words(self.settings.voice.stt.wake_words)
        if not norm or not phrase:
            return False, ""
        trigger = phrase[-1]                          # e.g. "otto"
        start = 1 if norm[0] in _GREETINGS else 0
        if start < len(norm) and _wordish(norm[start], trigger):
            return True, " ".join(norm[start + 1:])
        if _wordish(norm[0], trigger):                # trigger first, no greeting
            return True, " ".join(norm[1:])
        # fused whole phrase, e.g. "Hey Otto" -> "hiyoto" vs "heyotto"
        concat = "".join(phrase)
        if len(concat) >= 6 and _levenshtein(norm[0], concat) <= 2:
            return True, " ".join(norm[1:])
        return False, ""

    def _enter_awaiting_command(self) -> None:
        """Wake word heard alone — greet warmly and wait for the command."""
        self._awaiting_command = True
        self._status("listening", "Listening…")
        ack = random.choice(_WAKE_ACKS)
        self._last_ack = ack
        self.bus.transcript("assistant", ack, final=True)
        if self.mouth and not self.voice_muted:
            self.mouth.speak(ack)
        if self._loop is not None:
            if self._await_handle is not None:
                self._await_handle.cancel()
            self._await_handle = self._loop.call_later(12, self._await_timeout)

    def _is_ack_echo(self, text: str) -> bool:
        """Did the mic just hear Otto's own greeting? (avoid treating it as the command).

        Checks against ALL greeting phrases, so even a garbled/merged echo of one or more
        greetings is recognised rather than sent to the brain.
        """
        tw = set(_norm_words(text))
        if not tw:
            return True
        ack_words = set()
        for a in _WAKE_ACKS:
            ack_words |= set(_norm_words(a))
        return len(tw & ack_words) / len(tw) > 0.6

    def _await_timeout(self) -> None:
        if self._awaiting_command:
            self._awaiting_command = False
            self._status("idle", "Ready")

    def _cancel_await(self) -> None:
        self._awaiting_command = False
        if self._await_handle is not None:
            self._await_handle.cancel()
            self._await_handle = None

    def _echo_window(self) -> float:
        # Cover the whole pipeline after Otto stops: VAD end (silence_seconds) +
        # transcription lag + margin, so Otto's own reply doesn't come back as a command.
        return self.settings.voice.stt.silence_seconds + 2.5

    def _on_speech_start(self) -> None:
        if self.mic_muted:
            return
        # Otto hearing its own voice through the speaker — don't treat it as user speech.
        if self.mouth is not None and self.mouth.recently_active(self._echo_window()):
            return
        # Show that Otto is actively hearing you (menu-bar 👂 + dashboard orb).
        self._status("listening", "Listening…")

    def _on_recording_stop(self) -> None:
        # Speech ended; if a turn doesn't pick up shortly (e.g. wake word with no
        # command), drop back to idle so the indicator doesn't stick on "listening".
        if self._loop is None:
            return
        def _reset():
            if self.state == "listening":
                self._status("idle", "Ready")
        self._loop.call_later(2.5, _reset)

    def _on_ptt_press(self) -> None:
        # Global hotkey (⌘⇧J) arms capture of the next utterance — same as the Talk button.
        self.arm_ptt()

    def _on_ptt_release(self) -> None:
        pass  # VAD detects end-of-speech; nothing to do on release

    # -- the turn ----------------------------------------------------------
    async def _handle_turn(self, text: str) -> None:
        """Run a turn in the focused workspace (compat entry point)."""
        await self._run_turn(self.focused_ws, text)

    async def _run_turn(self, ws: Workspace, text: str) -> None:
        """Run one turn for a workspace. Different workspaces run concurrently (per-workspace
        lock). EVERY workspace streams its real work — the user's turn, tool calls, and reply —
        to its OWN chat window in the dashboard (events tagged with the workspace). Only the
        *focused* workspace is spoken aloud; the others work in the background and post a
        completion notification when done. This mirrors having several Claude windows open."""
        async with ws.lock:
            p = ws.project
            focused = p == self.focused
            print(f"[otto] turn -> {ws.name} (focused={focused}): {text!r}",
                  file=sys.stderr, flush=True)
            self.bus.transcript("user", text, final=True, ws=p)   # show it in this window
            if focused:
                self._status("thinking", "Thinking…")
                self._speech_buf = ""
            ws.state = "thinking"
            self._publish_workspaces()
            assistant_text = ""
            try:
                async for ev in ws.brain.send(text):
                    live = p == self.focused   # focus can change mid-turn
                    if ev["type"] == "delta":
                        chunk = ev["text"]
                        assistant_text += chunk
                        self.bus.assistant_delta(chunk, ws=p)     # into this window
                        if live:
                            self._feed_speech(chunk)              # ...but only speak the focused
                            self._status("speaking", "Speaking…")
                        ws.state = "speaking"
                    elif ev["type"] == "tool_use":
                        ws.state = "working"
                        # The tool call itself IS the visible work — show it in the window
                        # (that's the "chat window" feel). No spoken narration of tools.
                        self._announce_tool(ev["name"], ev.get("detail", ""), ws=p)
                        if live:
                            self._status("working", ev["name"])
                    elif ev["type"] == "result":
                        if ev.get("is_error"):
                            self.bus.notify(
                                "Brain error",
                                ev.get("error_text") or "The model returned an error.")
                        sid = ev.get("session_id")   # a new chat mints its id on turn 1
                        if sid:
                            try:
                                self._persist_session(p, sid)
                            except Exception:
                                pass
                            if p == self.focused:
                                self._publish_session()
            except Exception as e:
                print(f"[otto] turn error ({ws.name}): {e!r}", file=sys.stderr, flush=True)
                _log.exception("Turn failed in workspace %s", ws.name)
                self.bus.notify("Error", str(e))
            finally:
                reply = assistant_text.strip()
                print(f"[otto] turn done ({ws.name}), reply len={len(reply)}",
                      file=sys.stderr, flush=True)
                ws.state = "idle"
                ws.last_reply = reply
                if reply:
                    self.bus.transcript("assistant", reply, final=True, ws=p)
                if focused:
                    self._flush_speech()
                    self._status("idle", "Ready")
                else:
                    # Background window finished — nudge the user; its chat is there to switch to.
                    summary = reply.split("\n", 1)[0][:140] if reply else "Done."
                    self.notify(f"{ws.name} finished", summary)
                self._publish_workspaces()

    def _feed_speech(self, chunk: str) -> None:
        """Buffer deltas and speak whole sentences for natural prosody."""
        if not self.mouth or self.voice_muted:
            return
        self._speech_buf += chunk
        while True:
            m = _SENTENCE_END.match(self._speech_buf)
            if not m:
                break
            sentence = m.group(0)
            self._speech_buf = self._speech_buf[m.end():]
            self.mouth.speak(sentence.strip())

    def _flush_speech(self) -> None:
        if self.mouth and not self.voice_muted and self._speech_buf.strip():
            self.mouth.speak(self._speech_buf.strip())
        self._speech_buf = ""

    def _announce_tool(self, name: str, detail: str, ws: str | None = None) -> None:
        if name == "Task":
            self.bus.tool_use("subagent", detail, ws=ws)
        elif name.startswith("mcp__"):
            self.bus.tool_use(name.split("__")[-1], detail, ws=ws)
        else:
            self.bus.tool_use(name, detail, ws=ws)

    # -- AssistantContext (called by the agent's tools) --------------------
    async def say(self, text: str) -> None:
        self.bus.transcript("assistant", text, final=True)
        if self.mouth and not self.voice_muted:
            self.mouth.speak(text)

    async def ask_user(self, question: str) -> str:
        self.bus.publish({"type": "ask_user", "question": question})
        self.bus.transcript("assistant", question, final=True)
        if self.mouth and not self.voice_muted:
            self.mouth.speak(question)
        assert self._loop is not None
        self._pending_answer = self._loop.create_future()
        try:
            return await asyncio.wait_for(self._pending_answer, timeout=120)
        except asyncio.TimeoutError:
            return "(no response)"
        finally:
            self._pending_answer = None

    async def confirm(self, action: str) -> bool:
        answer = await self.ask_user(f"I'm about to {action}. Should I go ahead?")
        low = answer.strip().lower()
        return any(low == a or low.startswith(a + " ") or a in low.split()
                   for a in _AFFIRMATIVE)

    def render_canvas(self, content: str, fmt: str = "html") -> None:
        self.focused_ws.last_canvas = content
        self.bus.canvas(content, fmt, ws=self.focused)

    def set_status(self, text: str) -> None:
        self._status("working", text)

    # -- per-workspace routing (called by each workspace's tools via WorkspaceContext) -----
    # Every workspace streams into its OWN chat window (events tagged with the project); only
    # the focused workspace is spoken aloud.
    async def _ws_say(self, project: str, text: str) -> None:
        ws = self.workspaces.get(project)
        if ws is not None:
            ws.last_reply = text
        self.bus.transcript("assistant", text, final=True, ws=project)
        if project == self.focused and self.mouth and not self.voice_muted:
            self.mouth.speak(text)

    def _ws_render(self, project: str, content: str, fmt: str = "html") -> None:
        ws = self.workspaces.get(project)
        if ws is not None:
            ws.last_canvas = content
        self.bus.canvas(content, fmt, ws=project)

    def _ws_set_status(self, project: str, text: str) -> None:
        ws = self.workspaces.get(project)
        if ws is not None:
            ws.state = "working"
        if project == self.focused:
            self._status("working", text)
        else:
            self._publish_workspaces()

    async def _ws_ask_user(self, project: str, question: str) -> str:
        if project == self.focused:
            return await self.ask_user(question)
        name = self.workspaces[project].name if project in self.workspaces else project
        self.notify(f"{name} needs your input", question)
        return ("The user isn't watching this workspace right now. If you can proceed with a "
                "sensible default, do so; otherwise stop and leave this for when they focus it.")

    async def _ws_confirm(self, project: str, action: str) -> bool:
        if project == self.focused:
            return await self.confirm(action)
        # A background workspace can't get a spoken yes/no — decline and flag it. (With the
        # permission setting on "auto", this path isn't reached; consequential actions there
        # are auto-approved, so background workspaces run fully unattended.)
        name = self.workspaces[project].name if project in self.workspaces else project
        self.notify(f"{name} needs approval", f"It wants to {action}. Focus it to approve.")
        return False

    def notify(self, title: str, body: str = "") -> None:
        self.bus.notify(title, body)
        _log.warning("%s — %s", title, body) if body else _log.warning("%s", title)
        # Best-effort native macOS notification.
        try:
            safe = (body or "").replace('"', "'")
            st = title.replace('"', "'")
            subprocess.Popen(
                ["osascript", "-e", f'display notification "{safe}" with title "{st}"']
            )
        except Exception:
            pass
