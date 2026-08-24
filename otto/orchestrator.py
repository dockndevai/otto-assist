"""The orchestrator: the realtime loop tying ears -> brain -> mouth together, and the
bridge the agent's tools call back into (say / ask_user / render_canvas / confirm).

State machine per turn:  idle -> (listening) -> thinking -> speaking -> idle
Barge-in: any speech-start while speaking stops the mouth immediately.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from typing import Any

from .brain.agent import ClaudeBrain
from .brain.base import Brain
from .config import Settings, save_config
from .server.events import EventBus

_SENTENCE_END = re.compile(r"[^.!?\n]*[.!?\n]+")

# Common speech-to-text mishearings of the "otto" trigger word.
_WAKE_ALIASES = {
    "otto": {"otto", "auto", "oto", "ottos", "otho", "atto", "alto", "otter",
             "ottoh", "audo", "auddo", "ado", "ottto", "hotto"},
}


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
<style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;margin:0;padding:28px;color:#0b1220}
h2{margin:0 0 4px;font-size:20px}.sub{color:#6b7a90;font-size:13px;margin-bottom:20px}
.bar{height:30px;border-radius:6px;background:linear-gradient(90deg,#37c0ff,#1f7fb8);line-height:30px}
.row{display:grid;grid-template-columns:120px 1fr 60px;align-items:center;gap:10px;margin:10px 0}
.lbl{font-size:14px;font-weight:600}.val{font-size:13px;color:#456;text-align:right}</style>
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
        self.brain: Brain = self._make_brain()
        self.current_project = str(settings.project_path)
        self.mouth = None       # set in start() if voice output enabled
        self.ears = None        # set in start() if voice input enabled
        self._ptt = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._turn_lock = asyncio.Lock()
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

    def _make_brain(self) -> Brain:
        # Only Claude today; the Brain protocol lets other agents drop in later.
        return ClaudeBrain(self, self.settings)

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

        self._publish_project()
        self._status("idle", "Ready")

    async def stop(self) -> None:
        if self.ears:
            self.ears.stop()
        if self._ptt:
            self._ptt.stop()
        await self.brain.disconnect()

    def _publish_project(self) -> None:
        self.bus.publish({"type": "project", "path": self.current_project,
                          "name": self.current_project.rstrip("/").split("/")[-1]})

    async def switch_project(self, path: str) -> None:
        """Re-bind the brain to another project directory (resumes its latest session)."""
        from pathlib import Path
        p = str(Path(path).expanduser())
        if p == self.current_project:
            return
        self._status("thinking", f"Switching to {Path(p).name}…")
        await self.brain.disconnect()
        self.settings.brain.project_dir = p
        self.current_project = p
        self.brain = self._make_brain()
        await self.brain.connect()
        # Remember the choice for next launch.
        try:
            self._persist_project(p)
        except Exception:
            pass
        self._publish_project()
        self._status("idle", f"Ready · {Path(p).name}")
        await self.say(f"Switched to {Path(p).name}.")

    def _persist_project(self, path: str) -> None:
        import yaml
        from .config import config_path
        cfg_file = config_path()
        data = yaml.safe_load(cfg_file.read_text()) if cfg_file.exists() else {}
        data = data or {}
        data.setdefault("brain", {})["project_dir"] = path
        save_config(data)

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
        assert self._loop is not None
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

    def _on_voice_final(self, text: str) -> None:
        if self.mic_muted:
            return
        # "phrase" backend = software wake word: only act on speech that begins with the
        # wake phrase (e.g. "hey otto"), or on the command right after it.
        if self.settings.voice.stt.wakeword_backend == "phrase":
            if self._awaiting_command:
                self._cancel_await()
                self.submit_user_input(text)
                return
            matched, remainder = self._match_wake(text)
            if not matched:
                if self.state == "listening":
                    self._status("idle", "Ready")     # ignore ambient speech
                return
            if remainder.strip():
                self.submit_user_input(remainder)     # "hey otto, what time is it"
            else:
                self._enter_awaiting_command()        # just "hey otto" — wait for command
            return
        self.submit_user_input(text)

    def _match_wake(self, text: str) -> tuple[bool, str]:
        """Does `text` start with the wake phrase (fuzzily)? -> (matched, command).

        Speech-to-text often mishears the trigger word (e.g. "Otto" -> "Auto"), so we
        match the trigger word by small edit distance / known aliases rather than exactly.
        """
        norm = re.sub(r"[^a-z0-9 ]", "", text.lower()).split()
        phrase = re.sub(r"[^a-z0-9 ]", "", self.settings.voice.stt.wake_words.lower()).split()
        if not norm or not phrase:
            return False, ""
        trigger = phrase[-1]                          # e.g. "otto"
        start = 1 if norm[0] in ("hey", "hi", "ok", "okay", "hey!") else 0
        if start < len(norm) and _wordish(norm[start], trigger):
            return True, " ".join(norm[start + 1:])
        # also accept the trigger as the very first word even without a greeting
        if _wordish(norm[0], trigger):
            return True, " ".join(norm[1:])
        return False, ""

    def _enter_awaiting_command(self) -> None:
        self._awaiting_command = True
        self._status("listening", "Listening…")
        if self.mouth and not self.voice_muted:
            self.mouth.speak("Yes?")
        if self._loop is not None:
            self._await_handle = self._loop.call_later(8, self._await_timeout)

    def _await_timeout(self) -> None:
        if self._awaiting_command:
            self._awaiting_command = False
            self._status("idle", "Ready")

    def _cancel_await(self) -> None:
        self._awaiting_command = False
        if self._await_handle is not None:
            self._await_handle.cancel()
            self._await_handle = None

    def _on_speech_start(self) -> None:
        if self.mic_muted:
            return
        # Barge-in: cut off speech the moment the user starts talking.
        if self.mouth and self.mouth.is_playing():
            self.mouth.stop()
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
        self._on_speech_start()
        if self.ears:
            self.ears.trigger_ptt_start()

    def _on_ptt_release(self) -> None:
        if self.ears:
            self.ears.trigger_ptt_stop()

    # -- the turn ----------------------------------------------------------
    async def _handle_turn(self, text: str) -> None:
        async with self._turn_lock:
            self.bus.transcript("user", text, final=True)
            self._status("thinking", "Thinking…")
            self._speech_buf = ""
            assistant_text = ""
            try:
                async for ev in self.brain.send(text):
                    if ev["type"] == "delta":
                        chunk = ev["text"]
                        assistant_text += chunk
                        self.bus.assistant_delta(chunk)
                        self._feed_speech(chunk)
                        self._status("speaking", "Speaking…")
                    elif ev["type"] == "tool_use":
                        self._announce_tool(ev["name"], ev.get("detail", ""))
                    elif ev["type"] == "result":
                        if ev.get("is_error"):
                            self.bus.notify(
                                "Brain error",
                                ev.get("error_text") or "The model returned an error.",
                            )
            except Exception as e:
                self.bus.notify("Error", str(e))
            finally:
                self._flush_speech()
                if assistant_text.strip():
                    self.bus.transcript("assistant", assistant_text.strip(), final=True)
                self._status("idle", "Ready")

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

    def _announce_tool(self, name: str, detail: str) -> None:
        if name == "Task":
            self.bus.tool_use("subagent", detail)
        elif name.startswith("mcp__"):
            self.bus.tool_use(name.split("__")[-1], detail)
        else:
            self.bus.tool_use(name, detail)

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
        self.bus.canvas(content, fmt)

    def set_status(self, text: str) -> None:
        self._status("working", text)

    def notify(self, title: str, body: str = "") -> None:
        self.bus.notify(title, body)
        # Best-effort native macOS notification.
        try:
            safe = (body or "").replace('"', "'")
            st = title.replace('"', "'")
            subprocess.Popen(
                ["osascript", "-e", f'display notification "{safe}" with title "{st}"']
            )
        except Exception:
            pass
