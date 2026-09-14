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
import time
from typing import Any


from . import sessions
from .brain.base import Brain
from .config import OTTO_HOME, Settings, save_config
from .history import History
from .memory import Memory
from .server.events import EventBus
from .workspace import Workspace

_log = logging.getLogger("otto")

_SENTENCE_END = re.compile(r"[^.!?\n]*[.!?\n]+")

_SPEAK_CODEBLOCK = re.compile(r"```.*?```", re.S)

# Replies that mean "nothing to say" — never speak these aloud (they're what the model emits
# for garbled/no-op input; speaking them is just noise).
_DEAD_REPLIES = {
    "no response requested", "no response needed", "no response", "none",
    "no reply", "nothing to do", "no action needed", "understood", "ok", "okay",
}


def _speakable(text: str) -> str:
    """Strip markdown/URLs/code from text so TTS speaks words, not '[label](https://…)'.
    The model is *asked* to be brief and markdown-free; this enforces it for the spoken path
    (the full, original text still goes to the chat/screen)."""
    t = _SPEAK_CODEBLOCK.sub(" ", text or "")
    t = re.sub(r"`[^`]*`", " ", t)                       # inline code
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)     # [label](url) / image -> label
    t = re.sub(r"https?://\S+|www\.\S+", " ", t)         # bare URLs
    t = re.sub(r"(?m)^\s*[>#]+\s*", "", t)               # headings / block quotes
    t = re.sub(r"(?m)^\s*[*\-+]\s+", "", t)              # bullet markers
    t = re.sub(r"[*_`#|]", "", t)                        # residual emphasis/table pipes
    t = re.sub(r"\s+", " ", t).strip()
    return t

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
        # Persistent, cross-session memory (OTTO_HOME/memory): durable facts + rolling
        # summaries + saved documents per project, injected into every turn and learned from
        # automatically. The dashboard history lives inside it too, one log per project.
        self.memory = Memory(OTTO_HOME / "memory", settings)
        self.history = History(self.memory, lambda: self.focused)
        # Move any pre-existing flat history into the per-project store (once).
        self.history.migrate_legacy(OTTO_HOME / "history.jsonl", str(settings.project_path))
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
        self._external_ptt = False  # menu bar installs the global hotkey on the main thread
        self._eff = None        # EfficientWord-Net custom wake-word listener
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending_answer: asyncio.Future[str] | None = None
        self.remote_clients = 0      # authenticated phone clients currently connected (WS)
        self._speech_buf = ""
        self._spoken_chars = 0       # chars spoken so far this turn (for the spoken-length cap)
        self._speak_done = False     # cap reached → stop speaking the rest of this reply
        self._speaking = False
        # Live state + controls (read/driven by the menu bar app).
        self.state = "starting"
        self.mic_muted = False
        self.voice_muted = False
        # First-run permissions + startup/restart health check ("is Otto ready?").
        self.ready = False
        self.preflight_done = False
        self.preflight_checks: list = []
        # Software ("phrase") wake word: await the command after "hey otto".
        self._awaiting_command = False
        self._await_handle = None
        self._last_wake = 0.0     # debounce timestamp for wake-word detections
        self._last_ack = ""     # last greeting spoken (to ignore its own echo)
        # Push-to-talk: capture the next utterance as a command, no wake word needed.
        self._ptt_armed = False
        self._ptt_handle = None
        # Hands-free "sleep": in always-listen, saying "stop listening" pauses Otto until the
        # wake word wakes it again (then it's fully hands-free once more). The mic stays on so
        # it can still hear the wake word; everything else is ignored while asleep.
        self._sleeping = False
        # Dictation-anywhere: when on, a heard command is typed into the focused app instead
        # of being sent to Claude (runtime-toggleable; starts from config).
        self._dictation = settings.voice.stt.dictation_mode
        # True while a post-reply follow-up window is open (hands-free) — those utterances get
        # the stricter anti-hallucination gate; an after-wake command does not.
        self._follow_up_active = False
        # Half-duplex guard: on a laptop speaker the mic hears Otto's OWN voice and would
        # answer itself in a loop. Primary defense: physically turn the mic OFF at the recorder
        # while Otto speaks (set_mic) so nothing is captured; `_mute_input_until` tracks when to
        # turn it back on, and also filters any straggler transcript as a backstop.
        self._mute_input_until = 0.0
        self._unmute_handle = None

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

    # -- half-duplex voice output -----------------------------------------
    def _voice_out(self, text: str) -> None:
        """Speak `text` (when voice is on) and push out the input-mute window by roughly how
        long it takes to say it, plus a cooldown — so Otto never transcribes its own voice."""
        t = (text or "").strip()
        if not t or self.mouth is None or self.voice_muted:
            return
        # When a phone is connected it speaks the reply itself — don't also play it aloud on
        # the Mac (talking to an empty room when you're remote). The local dashboard doesn't count.
        if self.remote_clients > 0 and self.settings.voice.tts.mute_when_remote:
            return
        self.mouth.speak(t)
        # ~14 chars/sec for macOS `say`, + a cooldown for the speaker/room tail after it stops.
        secs = max(1.0, len(t) / 14.0) + 2.5
        self._mute_input_until = max(self._mute_input_until, time.monotonic() + secs)
        # Primary echo cure: physically stop the mic from capturing while we speak, then
        # re-enable it once the mute window elapses (extended by any later line).
        if self.ears is not None:
            try:
                self.ears.set_mic(False)
            except Exception:
                pass
            self._schedule_unmute()

    # A quick spoken acknowledgment the instant a command lands — so Otto feels alive and
    # responsive (Jarvis-style "On it"), and the wait for Claude's real reply isn't dead air.
    # The ack is chosen to fit the intent: an action gets "On it.", a question gets a
    # thinking beat, so it sounds like it actually understood you rather than a canned bark.
    _ACKS_DO = ("On it.", "Right away.", "Sure thing.", "Got it.", "Okay—", "Consider it done.")
    _ACKS_ASK = ("Let me check.", "One sec.", "Looking into it.", "Let me see.", "Checking.")

    def _ack(self, intent: str = "command") -> None:
        import random
        pool = self._ACKS_ASK if intent == "question" else self._ACKS_DO
        self._voice_out(random.choice(pool))

    # -- intent identification -------------------------------------------------
    # Before doing any work, decide what KIND of thing you just said, so Otto reacts like a
    # person would: chit-chat gets a warm instant reply (no Claude round-trip, no "On it."),
    # a question gets a thinking ack + an answer, an action gets "On it." + the work.
    _CASUAL_EXACT = {
        "hi", "hey", "hello", "yo", "hiya", "hey there", "hi there", "hello there",
        "good morning", "morning", "good afternoon", "good evening", "good night",
        "goodnight", "night", "how are you", "how are you doing", "how's it going",
        "hows it going", "how you doing", "what's up", "whats up", "sup", "you there",
        "you awake", "are you there", "are you awake", "thanks", "thank you",
        "thanks a lot", "thank you so much", "cheers", "ta", "much appreciated",
        "nice", "cool", "great", "awesome", "perfect", "sweet", "lovely", "nice one",
        "good job", "well done", "good work", "bye", "goodbye", "see you", "see ya",
        "talk later", "later", "ok", "okay", "alright", "got it", "sounds good",
        "never mind", "nevermind", "forget it", "no worries", "you good", "howdy",
    }
    _QUESTION_STARTS = (
        "what", "why", "how", "when", "where", "who", "whom", "whose", "which",
        "can you", "could you", "would you", "will you", "do you", "does", "did",
        "is ", "are ", "am i", "should", "have you", "has ", "was ", "were ",
        "tell me", "explain", "describe", "any idea", "do we", "is there", "are there",
    )

    def _classify_intent(self, text: str) -> str:
        """Return 'casual' (chit-chat / social), 'question', or 'command' (an action to do).

        Deliberately conservative: casual only fires on a whole-utterance social phrase, so a
        real request that merely contains a greeting ("hi, open the repo") is never swallowed."""
        norm = " ".join(_norm_words(text))
        if not norm:
            return "casual"
        if norm in self._CASUAL_EXACT:
            return "casual"
        # A short social opener followed by nothing substantive ("hey otto" already stripped).
        if len(norm.split()) <= 3 and norm.split()[0] in {
                "hi", "hey", "hello", "yo", "thanks", "cheers", "bye", "morning", "howdy"}:
            return "casual"
        if text.strip().endswith("?") or norm.startswith(self._QUESTION_STARTS):
            return "question"
        return "command"

    # Warm, instant, local replies for chit-chat — no Claude turn, so it feels alive and human
    # instead of spinning up a full agent run just to say hello.
    _CASUAL_REPLIES = {
        "greet": ("Hey — what are we working on?", "Hi there. What can I do?",
                  "Hey. Ready when you are.", "Yes? I'm here."),
        "how": ("Doing great — ready when you are.", "All good here. What's up?",
                "Running smooth. What do you need?"),
        "thanks": ("Anytime.", "You got it.", "Happy to help.", "Of course."),
        "praise": ("Thanks!", "Appreciated.", "Glad it landed."),
        "bye": ("Talk soon.", "Catch you later.", "I'll be here."),
        "ack": ("Okay.", "Got it.", "Sure."),
    }

    def _casual_reply(self, text: str) -> str:
        import random
        norm = " ".join(_norm_words(text))
        if any(w in norm for w in ("thank", "cheers", "appreciat", "much oblig")):
            key = "thanks"
        elif any(w in norm for w in ("how are", "how's it", "hows it", "how you", "what's up",
                                     "whats up", "sup", "you there", "you awake", "you good")):
            key = "how"
        elif any(w in norm for w in ("nice", "cool", "great", "awesome", "perfect", "sweet",
                                     "lovely", "good job", "well done", "good work")):
            key = "praise"
        elif any(w in norm for w in ("bye", "goodbye", "see you", "see ya", "later", "goodnight",
                                     "good night", "night")):
            key = "bye"
        elif norm.split() and norm.split()[0] in {"hi", "hey", "hello", "yo", "hiya", "howdy",
                                                  "morning", "good"}:
            key = "greet"
        else:
            key = "ack"
        return random.choice(self._CASUAL_REPLIES[key])

    def _schedule_unmute(self) -> None:
        if self._loop is None:
            return
        if self._unmute_handle is not None:
            self._unmute_handle.cancel()
        delay = max(0.1, self._mute_input_until - time.monotonic())
        self._unmute_handle = self._loop.call_later(delay, self._do_unmute)

    def _do_unmute(self) -> None:
        # A later spoken line may have pushed the window out — only re-enable once it's truly past.
        if time.monotonic() < self._mute_input_until - 0.05:
            self._schedule_unmute()
            return
        # The char-based window is tuned for macOS `say`; Kokoro (synth + afplay) can run past
        # it. Stay muted while audio is actually still playing so Orrin never hears itself.
        try:
            if self.mouth is not None and self.mouth.is_playing():
                self._mute_input_until = time.monotonic() + 0.6
                self._schedule_unmute()
                return
        except Exception:
            pass
        self._unmute_handle = None
        if self.ears is not None and not self.mic_muted:
            try:
                self.ears.set_mic(True)
            except Exception:
                pass

    def _output_suppressing(self) -> bool:
        """True while Otto is speaking (or in the cooldown after) — drop mic input then."""
        if self.mouth is None:
            return False
        try:
            if self.mouth.is_playing():
                return True
        except Exception:
            pass
        return time.monotonic() < self._mute_input_until

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._status("starting", "Checking setup…")
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

        # Microphone. The recorder runs in a multiprocessing CHILD (a re-exec of the signed
        # binary) that TCC will NOT prompt for on its own — so the MAIN app must hold the grant
        # and the child inherits it. The menu-bar (AppKit main thread) asks for it via
        # on_mic_decision(); we only start the ears once we have access. If it's already granted
        # we start immediately; if it's undecided we wait for the prompt result; if it's blocked
        # we say so. (The mic prompt only renders from the main run loop, which is why the ask
        # lives in the menu-bar, not here on the backend thread.)
        if v.input_enabled:
            from . import preflight
            st = preflight.mic_authorization_status()
            if st == "authorized":
                self._start_ears()
            elif st in ("denied", "restricted"):
                self.ears = None
                self.bus.notify(
                    "Microphone blocked",
                    "Enable Orrin under System Settings → Privacy & Security → Microphone, "
                    "then click Recheck.")
            # notDetermined/unknown: wait for the menu-bar's main-thread prompt (on_mic_decision).

        self._publish_project()
        self._publish_session()
        # Usable immediately; the full health check (incl. a real Claude test turn) runs in the
        # background and flips the dashboard's "Otto ready" panel to green when it passes. This
        # runs on every start(), so it re-validates on each restart too.
        self._status("idle", "Ready")
        self._loop.create_task(self.run_preflight(request=False))

    def on_mic_decision(self, granted: bool) -> None:
        """Called (via the menu-bar's main-thread mic prompt) once the user decides. With the
        grant in hand the recorder child can open the input device, so start the ears now."""
        if not self.settings.voice.input_enabled:
            return
        if granted:
            if self.ears is None:
                self._start_ears()
        else:
            self.ears = None
            self.bus.notify(
                "Microphone blocked",
                "Enable Orrin under System Settings → Privacy & Security → Microphone, "
                "then click Recheck.")
        # Reflect the new mic state (and readiness) in the dashboard panel.
        if self._loop is not None:
            self._loop.create_task(self.run_preflight())

    def _start_ears(self) -> None:
        """Bring up the ears (+ optional wake-word listener / push-to-talk hotkey). Idempotent."""
        if self.ears is not None:
            return
        v = self.settings.voice
        try:
            from .voice.stt import Ears
            self.ears = Ears(
                self.settings, self._loop,
                on_final=self._on_voice_final,
                on_partial=lambda t: self.bus.transcript("user", t, final=False),
                on_speech_start=self._on_speech_start,
                on_wake=self._on_speech_start,
                on_recording_stop=self._on_recording_stop,
                on_realtime=self._on_voice_partial,
            )
            self.ears.start()
            # Global push-to-talk. Under the menu-bar app the hotkey is owned there and
            # installed on the MAIN thread (NSEvent), because pynput's macOS input-source
            # calls run off the main thread and hard-abort (SIGTRAP) the packaged app. Here we
            # only start the pynput listener for a bare terminal run (no menu bar), where it's
            # safe. `_external_ptt` is set by the menu bar.
            if v.hotkey.enabled and not self._external_ptt:
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

    # -- preflight / readiness --------------------------------------------
    def _publish_preflight(self, done: bool) -> None:
        from . import preflight
        self.bus.publish({
            "type": "preflight",
            "checks": [c.as_dict() for c in self.preflight_checks],
            "ready": self.ready,
            "done": done,
            "first_run": preflight.is_first_run(),
        })

    async def run_preflight(self, *, request: bool = False) -> None:
        """Validate everything Otto needs and publish the results, streaming each check as it
        resolves so the dashboard animates. ``request=True`` also (re-)asks for the mic prompt
        (used by the dashboard's Recheck button after the user grants access)."""
        from . import preflight
        assert self._loop is not None
        self.preflight_done = False
        self.preflight_checks = [
            preflight.Check("microphone", "Microphone access",
                            required=self.settings.voice.input_enabled, pane="microphone"),
            preflight.Check("claude_cli", "Claude CLI found"),
            preflight.Check("claude_test", "Claude connection (test run)"),
            preflight.Check("voice_output", "Voice output", required=False),
            preflight.Check("accessibility", "Input Monitoring (global hotkey)",
                            required=self.settings.voice.hotkey.enabled,
                            pane="input_monitoring"),
        ]
        self._publish_preflight(done=False)
        # NB: we deliberately do NOT call AVFoundation requestAccess (see start()); the recorder
        # opening the mic is what triggers the real prompt. `request` is kept for API compat.

        def _set(c) -> None:
            for i, x in enumerate(self.preflight_checks):
                if x.key == c.key:
                    self.preflight_checks[i] = c
                    break
            self._publish_preflight(done=False)

        _set(preflight.check_microphone(self.settings))
        _set(preflight.check_claude_cli(self.settings))
        _set(preflight.check_voice_output(self.settings))
        _set(preflight.check_accessibility(self.settings))
        _set(await preflight.check_claude_test(self.settings))   # the slow one, last

        # If the ears never came up and the mic isn't explicitly blocked, (re)start them now —
        # e.g. permission granted after launch, then Recheck. No restart needed.
        if (self.ears is None and self.settings.voice.input_enabled
                and preflight.mic_authorization_status() not in ("denied", "restricted")):
            self._start_ears()
            _set(preflight.check_microphone(self.settings))

        self.ready = preflight.summarize(self.preflight_checks)
        self.preflight_done = True
        if self.ready:
            preflight.mark_setup_complete()
        self._publish_preflight(done=True)

    # -- voice selection ---------------------------------------------------
    def set_voice(self, voice: str) -> None:
        """Switch Otto's spoken voice live and persist it. The field depends on the engine:
        `voice` for macOS `say`, `kokoro_voice` for the local neural voice."""
        voice = (voice or "").strip()
        key = "kokoro_voice" if self.settings.voice.tts.engine == "kokoro" else "voice"
        setattr(self.settings.voice.tts, key, voice)
        if self.mouth is not None:
            try:
                self.mouth.set_voice(voice)
            except Exception:
                pass
        # Also remember it as THIS project's voice, so parallel workspaces can differ and the
        # choice sticks when you switch away and back.
        self.settings.voice.tts.project_voices[self.focused] = voice
        try:
            import yaml
            from .config import config_path
            cfg_file = config_path()
            data = yaml.safe_load(cfg_file.read_text()) if cfg_file.exists() else {}
            data = data or {}
            tts = data.setdefault("voice", {}).setdefault("tts", {})
            tts[key] = voice
            tts.setdefault("project_voices", {})[self.focused] = voice
            save_config(data)
        except Exception:
            pass

    def preview_voice(self, voice: str) -> None:
        """Speak a short sample in `voice` so the user can hear it before choosing."""
        sample = "Hi, I'm Otto. This is how I'll sound."
        if self.mouth is not None:
            try:
                self.mouth.preview(sample, (voice or "").strip())
            except Exception:
                pass

    def open_privacy_pane(self, which: str) -> bool:
        from . import preflight
        if which == "accessibility":
            preflight.prompt_accessibility()
            return True
        return preflight.open_privacy_pane(which)

    def _on_eff_wake(self) -> None:
        # While asleep ("stop listening"), ignore the acoustic detector entirely — it false-fires
        # on ambient/echo and would wake Otto right back. Waking from sleep goes through the
        # transcript wake-word match instead (see the _sleeping branch in _on_voice_final), so a
        # spoken "hey otto" still wakes it, but noise can't.
        if self._sleeping:
            return
        # Echo guard: the wake listener has its own mic and hears Otto's own voice through the
        # speaker — ignore detections while Otto is speaking or just spoke (widened window).
        if self.mouth is not None and self.mouth.recently_active(window=2.5):
            return
        if self._awaiting_command:
            return   # already listening for the command; don't re-fire
        # Debounce: the detector can fire repeatedly on echo/ambient — one activation per window.
        now = time.monotonic()
        if now - self._last_wake < self.settings.voice.stt.wake_debounce_seconds:
            return
        self._last_wake = now
        print("[otto] wake word detected", file=sys.stderr, flush=True)
        # Silent by default (no repetitive spoken greeting that also feeds the detector); just
        # start listening. Config voice.stt.wake_greeting re-enables the spoken ack.
        self._enter_awaiting_command(greet=self.settings.voice.stt.wake_greeting)

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
        self._apply_project_voice()      # speak this workspace in its own voice
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

    async def open_session_anywhere(self, project: str, session_id: str) -> None:
        """Open ANY chat from ANY project (incl. cowork) in one step: focus that project as a
        workspace already bound to the chosen session. This is the 'all sessions access' path."""
        from pathlib import Path
        project = str(Path(project).expanduser())
        session_id = (session_id or "").strip()
        if not session_id:
            return
        if project in self.workspaces:          # already open — just focus + switch chat
            self.focus_workspace(project)
            await self.switch_session(session_id)
            return
        self._status("thinking", f"Opening {Path(project).name}…")
        if len(self.workspaces) >= self.max_workspaces:
            await self.close_workspace(self.focused)   # swap out at the cap (like switch_project)
        ws = Workspace(self, self.settings, project, resume=session_id)
        await ws.brain.connect()                # bound to the requested chat from the start
        self.workspaces[project] = ws
        self.focus_workspace(project)
        try:
            self.settings.brain.project_dir = project
            self._persist_project(project)
            self._persist_session(project, session_id)
        except Exception:
            pass
        self._publish_session()
        self._status("idle", f"Ready · {Path(project).name}")
        await self.say("Opened that chat.")

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
        """Resolve a spoken chat name to a session and switch, or ask to disambiguate.

        Searches the focused project first; if nothing matches there, falls back to EVERY
        project on the machine (cowork included) so you can jump to any chat by voice."""
        needle = name.lower().strip()
        chats = sessions.list_sessions(self.current_project,
                                       current_id=self._active_session)
        matches = [s for s in chats if needle in s.title.lower()]
        if len(matches) == 1:
            await self.switch_session(matches[0].session_id)
            return
        if len(matches) > 1:
            names = ", ".join(s.title for s in matches[:4])
            await self.say(f"I found a few chats matching that: {names}. Which one?")
            return
        # Nothing in this project — search across all projects.
        allm = sessions.list_all_sessions(query=needle, current_id=self._active_session)
        if not allm:
            await self.say(f"I couldn't find a chat matching “{name}” in any project.")
            return
        if len(allm) > 1:
            names = ", ".join(f"{m['title']} in {m['project_name']}" for m in allm[:3])
            await self.say(f"I found a few across your projects: {names}. Which one?")
            return
        m = allm[0]
        await self.say(f"Opening {m['title']} in {m['project_name']}.")
        await self.open_session_anywhere(m["project"], m["session_id"])

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
        self._mute_input_until = 0.0     # explicit Talk press bypasses the half-duplex mute
        if self.ears is not None:        # make sure the mic is on for the press
            try:
                self.ears.set_mic(True)
            except Exception:
                pass
        if self._unmute_handle is not None:
            self._unmute_handle.cancel()
            self._unmute_handle = None
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
        # Log every heard utterance (transcript-level captures), even ones we ignore below —
        # useful for reviewing mis-hears in always-listen.
        try:
            self.memory.record_capture(self.focused, text, source="voice")
        except Exception:
            pass
        # Hands-free dictation toggle ("start/stop dictation") — whole-utterance match.
        if self._is_dictation_command(text):
            self.set_dictation(not self._dictation)
            return
        # Orrin asked a question (ask_user/confirm) and is waiting: take this utterance as the
        # answer directly — NO wake word needed, in any mode. Ignore the echo of the question
        # itself (mic may still catch the tail of Orrin speaking it).
        if self._pending_answer is not None and not self._pending_answer.done():
            if self._output_suppressing():        # mic tail of Orrin speaking the question
                print(f"[otto] ignoring echo while awaiting answer: {text!r}",
                      file=sys.stderr, flush=True)
                return
            print(f"[otto] answer to prompt: {text!r}", file=sys.stderr, flush=True)
            self.submit_user_input(text)
            return
        # Push-to-talk armed: this utterance is the command, no wake word required.
        if self._ptt_armed:
            self._ptt_armed = False
            if self._ptt_handle is not None:
                self._ptt_handle.cancel()
            print(f"[otto] ptt heard: {text!r}", file=sys.stderr, flush=True)
            self._dispatch_voice_command(text, deliberate=True)   # button press = trust it
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
            hands_free = self._follow_up_active     # a follow-up window is hands-free; after a
            self._cancel_await()                    # bare wake word it's deliberate
            print(f"[otto] command after wake: {command!r} (hands_free={hands_free})",
                  file=sys.stderr, flush=True)
            self._dispatch_voice_command(command, hands_free=hands_free)
            return
        # Half-duplex echo guard: while Otto is speaking (and for a content-length-based
        # cooldown after), the mic hears Otto's own voice — drop it so Otto doesn't answer
        # itself in a loop. This is essential on a laptop speaker (no headphones).
        if self._output_suppressing():
            print(f"[otto] ignoring self-echo (speaking): {text!r}", file=sys.stderr, flush=True)
            return
        # Asleep (paused by "stop listening"): ignore everything EXCEPT the wake word, which
        # wakes Otto back into fully hands-free listening. If a command trails the wake word
        # ("otto, what's the weather"), run it immediately.
        if self._sleeping:
            matched, remainder = self._match_wake(text)
            if not matched:
                print(f"[otto] asleep, ignoring: {text!r}", file=sys.stderr, flush=True)
                return
            self._sleeping = False
            print(f"[otto] woke from sleep: {text!r}", file=sys.stderr, flush=True)
            if remainder.strip():
                self._dispatch_voice_command(remainder)
            else:
                ack = random.choice(_WAKE_ACKS)
                self.bus.transcript("assistant", ack, final=True)
                self._status("listening", "Listening…")
                self._voice_out(ack)
            return
        # "stop listening" — go quiet until the wake word (works while always-listening).
        if self._is_sleep_command(text):
            self._enter_sleep()
            return
        # Always-listen: no wake word, just respond to whatever you say (self-echo already
        # filtered above). Mute the mic when you don't want Otto listening.
        if self.settings.voice.stt.always_listen:
            print(f"[otto] always-listen: {text!r}", file=sys.stderr, flush=True)
            self._dispatch_voice_command(text, hands_free=True)   # gate ambient hallucinations
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
                self._dispatch_voice_command(remainder)   # "hey otto, what time is it"
            else:
                self._enter_awaiting_command(         # just "hey otto" — wait for command
                    greet=self.settings.voice.stt.wake_greeting)
            return
        if self.settings.voice.stt.wakeword_backend == "efficientword":
            if self._eff is not None:
                return   # wake handled acoustically by EffWakeWord -> greet + await; ignore rest
            # The acoustic listener never came up (deps missing / failed to load). Don't go
            # silently deaf: degrade to software phrase matching so speech is still acted on.
            matched, remainder = self._match_wake(text)
            print(f"[otto] heard (eff-fallback): {text!r} | matched={matched} cmd={remainder!r}",
                  file=sys.stderr, flush=True)
            if not matched:
                if self.state == "listening":
                    self._status("idle", "Ready")
                return
            if remainder.strip():
                self._dispatch_voice_command(remainder)
            else:
                self._enter_awaiting_command(greet=self.settings.voice.stt.wake_greeting)
            return
        self._dispatch_voice_command(text)

    # Utterances that put Otto to sleep (checked after stripping a leading greeting/wake word,
    # so "otto, stop listening" works too). Whole-utterance match to avoid false triggers.
    _SLEEP_PHRASES = {
        "stop listening", "stop listning", "stop listen", "go to sleep", "goto sleep",
        "go to sleep now", "pause listening", "stand by", "standby", "mute yourself",
        "go quiet", "be quiet", "stop for now", "sleep", "sleep now", "take a break",
    }

    def _is_sleep_command(self, text: str) -> bool:
        words = _norm_words(text)
        trigger = (_norm_words(self.settings.voice.stt.wake_words)[-1:] or ["otto"])[0]
        # peel a leading greeting and/or the wake word ("hey otto stop listening")
        while words and (words[0] in _GREETINGS or _wordish(words[0], trigger)):
            words = words[1:]
        return " ".join(words) in self._SLEEP_PHRASES

    def _enter_sleep(self) -> None:
        """Pause hands-free listening until the wake word. The mic stays on for the wake word."""
        self._sleeping = True
        self._cancel_await()
        ack = "Okay, going quiet. Say the wake word when you need me."
        self.bus.transcript("assistant", ack, final=True)
        self._status("idle", "Sleeping — say the wake word")
        self._voice_out(ack)

    # -- voice command dispatch (refine + dictation) ----------------------
    def _dispatch_voice_command(self, text: str, deliberate: bool = False,
                                hands_free: bool = False) -> None:
        """Route a heard command through the anti-hallucination filter, optional refine, then
        dictation or Claude.

        `deliberate` (push-to-talk) skips the noise filter entirely. Otherwise the free
        heuristic drops obvious junk; on the `hands_free` path (always-listen / follow-up) a
        model gate also rejects grammatical-but-nonsensical hallucinations. Fast synchronous
        path when nothing async is needed (unchanged latency for wake-word/PTT)."""
        stt = self.settings.voice.stt
        if not deliberate and stt.filter_hallucinations:
            from .voice.noise import is_noise
            if is_noise(text):
                print(f"[otto] dropped noise: {text!r}", file=sys.stderr, flush=True)
                return
        # Semantic gate on the hands-free path (always-listen / follow-up) — the noisiest,
        # lowest-intent context. After a deliberate wake word we stay snappy (no model call);
        # any junk that still slips through is caught by the dead-reply suppression on output.
        need_verify = hands_free and stt.hands_free_verify and not deliberate
        # Intent first: react like a person, not a switch. Chit-chat ("hey", "how are you",
        # "thanks") gets a warm INSTANT reply right here — no Claude round-trip, no "On it." —
        # then a short hands-free window so you can keep talking. A question or an action flows
        # on to Claude with an ack that fits what you asked. Dictation types verbatim, so it
        # opts out of intent shaping.
        intent = "command"
        if not self._dictation:
            intent = self._classify_intent(text)
            if intent == "casual":
                reply = self._casual_reply(text)
                print(f"[otto] casual: {text!r} -> {reply!r}", file=sys.stderr, flush=True)
                self.bus.transcript("user", text, final=True)
                self.bus.transcript("assistant", reply, final=True)
                self._status("idle", "Ready")
                self._voice_out(reply)
                # Keep the conversation open, hands-free — no need to say the wake word again.
                self._enter_awaiting_command(greet=False, follow_up=True)
                return
        if not stt.refine and not self._dictation and not need_verify:
            self._ack(intent); self.submit_user_input(text)   # fast path — ack, no model calls
            return
        if self._loop is not None:
            self._loop.create_task(self._dispatch_async(text, need_verify, intent))
        else:
            self.submit_user_input(text)

    async def _dispatch_async(self, text: str, verify: bool = False,
                              intent: str = "command") -> None:
        if verify:
            try:
                from .voice.noise import is_real_command
                if not await is_real_command(text, model=self.settings.voice.stt.verify_model):
                    print(f"[otto] dropped (not a command): {text!r}",
                          file=sys.stderr, flush=True)
                    return
            except Exception:
                pass
        if self.settings.voice.stt.refine:
            try:
                from .voice.refine import refine_transcript
                text = await refine_transcript(
                    text, model=self.settings.voice.stt.refine_model)
            except Exception:
                pass
        if self._dictation:
            try:
                from .voice.dictation import paste_text
                self.bus.transcript("user", text, final=True)   # show what was dictated
                ok = await asyncio.to_thread(paste_text, text)
                if not ok:
                    self.notify("Dictation", "Couldn't type that — is Accessibility granted?")
            except Exception as e:
                self.notify("Dictation error", str(e))
            return
        self._ack(intent); self.submit_user_input(text)   # acknowledge, then run the turn

    _DICTATION_ON = {"start dictation", "start dictating", "dictation mode",
                     "dictation on", "start typing", "type what i say"}
    _DICTATION_OFF = {"stop dictation", "stop dictating", "dictation off",
                      "exit dictation", "stop typing", "talk to claude"}

    def _is_dictation_command(self, text: str) -> bool:
        words = _norm_words(text)
        trigger = (_norm_words(self.settings.voice.stt.wake_words)[-1:] or ["otto"])[0]
        while words and (words[0] in _GREETINGS or _wordish(words[0], trigger)):
            words = words[1:]
        phrase = " ".join(words)
        return phrase in self._DICTATION_ON or phrase in self._DICTATION_OFF

    def set_dictation(self, on: bool) -> None:
        """Turn dictation-anywhere on/off (persisted). In dictation mode, speech is typed into
        the focused app instead of sent to Claude."""
        self._dictation = bool(on)
        try:
            self.settings.voice.stt.dictation_mode = self._dictation
            self._persist_setting(("voice", "stt", "dictation_mode"), self._dictation)
        except Exception:
            pass
        ack = ("Dictation on — I'll type what you say into the focused app."
               if self._dictation else "Dictation off — back to talking with Claude.")
        self.bus.transcript("assistant", ack, final=True)
        self._status("idle", "Dictation" if self._dictation else "Ready")
        self._voice_out(ack)

    # -- per-project (workspace) voice ------------------------------------
    def _apply_project_voice(self) -> None:
        """Speak the focused project in its configured voice (falls back to the default)."""
        if self.mouth is None:
            return
        tts = self.settings.voice.tts
        default = tts.kokoro_voice if tts.engine == "kokoro" else tts.voice
        voice = tts.project_voices.get(self.focused, default)
        try:
            self.mouth.set_voice(voice or "")
        except Exception:
            pass

    def _persist_setting(self, path: tuple[str, ...], value) -> None:
        """Write a single nested key into the on-disk config.yaml."""
        import yaml
        from .config import config_path
        cfg_file = config_path()
        data = yaml.safe_load(cfg_file.read_text()) if cfg_file.exists() else {}
        data = data or {}
        node = data
        for k in path[:-1]:
            node = node.setdefault(k, {})
        node[path[-1]] = value
        save_config(data)

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

    def _enter_awaiting_command(self, greet: bool = True, timeout: float = 12,
                                follow_up: bool = False) -> None:
        """Wait for a command with no wake word. `greet` speaks a short ack; `follow_up` marks a
        post-reply window (hands-free, stricter noise gate). A wake-word activation is not a
        follow-up even when it's silent — the two are independent."""
        self._awaiting_command = True
        self._follow_up_active = follow_up
        self._status("listening", "Listening…")
        if greet:
            ack = random.choice(_WAKE_ACKS)
            self._last_ack = ack
            self.bus.transcript("assistant", ack, final=True)
            self._voice_out(ack)
        if self._loop is not None:
            if self._await_handle is not None:
                self._await_handle.cancel()
            self._await_handle = self._loop.call_later(timeout, self._await_timeout)

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
            self._follow_up_active = False
            self._status("idle", "Ready")

    def _cancel_await(self) -> None:
        self._awaiting_command = False
        self._follow_up_active = False
        if self._await_handle is not None:
            self._await_handle.cancel()
            self._await_handle = None

    def _echo_window(self) -> float:
        # Cover the whole pipeline after Otto stops: VAD end (silence_seconds) +
        # transcription lag + margin, so Otto's own reply doesn't come back as a command.
        return self.settings.voice.stt.silence_seconds + 2.5

    def _on_voice_partial(self, text: str) -> None:
        """Live caption while you speak — stream partial words to the dashboard. Suppressed
        while Otto is speaking (echo) or muted/asleep."""
        if self.mic_muted or self._sleeping:
            return
        if self._output_suppressing():
            return
        t = (text or "").strip()
        if t:
            self.bus.transcript("user", t, final=False)

    def _on_speech_start(self) -> None:
        if self.mic_muted:
            return
        # Otto hearing its own voice through the speaker — don't treat it as user speech.
        if self._output_suppressing():
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
                self._spoken_chars = 0
                self._speak_done = False
            ws.state = "thinking"
            self._publish_workspaces()
            assistant_text = ""
            # Prepend durable memory (facts + recent summary + relevant saved-note excerpts)
            # so Claude has cross-session context. The dashboard/transcript already showed the
            # user's *clean* text above; only what we send to the brain carries the
            # (clearly-delimited) preamble. Optimization: within one resumed chat the model
            # already retains the earlier preamble, so we only (re)inject on the first turn or
            # when the remembered content has actually changed — avoiding redundant tokens every
            # turn. Doc-retrieval excerpts vary by turn, so those still refresh naturally.
            try:
                pre = self.memory.preamble(p, query=text)
                if pre and pre != getattr(ws, "_last_preamble", None):
                    send_text = pre + text
                    ws._last_preamble = pre
                else:
                    send_text = text
            except Exception:
                send_text = text
            try:
                async for ev in ws.brain.send(send_text):
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
                        # Only persist when the chat id actually changed — it's stable after
                        # turn 1, so writing config.yaml every turn was needless disk + YAML
                        # work on the loop.
                        if sid and sid != getattr(ws, "_persisted_sid", None):
                            ws._persisted_sid = sid
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
                    # Learn from the exchange in the background (records the turn + folds any
                    # durable facts/summary into the store). Off the voice path, never blocks.
                    if self._loop is not None:
                        self._loop.create_task(
                            self.memory.capture(p, text, reply, session_id=ws.session_id))
                if focused:
                    self._flush_speech()
                    # Open a silent follow-up window so the next command needs no wake word —
                    # unless we're asleep, dictating, or awaiting an explicit answer.
                    fu = self.settings.voice.stt.follow_up_seconds
                    if (fu and fu > 0 and reply and not self._sleeping
                            and not self._dictation
                            and not (self._pending_answer and not self._pending_answer.done())):
                        self._enter_awaiting_command(greet=False, follow_up=True, timeout=fu)
                    else:
                        self._status("idle", "Ready")
                else:
                    # Background window finished — nudge the user; its chat is there to switch to.
                    summary = reply.split("\n", 1)[0][:140] if reply else "Done."
                    self.notify(f"{ws.name} finished", summary)
                self._publish_workspaces()

    def _feed_speech(self, chunk: str) -> None:
        """Buffer deltas and speak whole sentences for natural prosody — cleaned + length-capped
        so Orrin answers in a sentence or two, not by narrating the whole (markdown) reply."""
        if not self.mouth or self.voice_muted:
            return
        self._speech_buf += chunk
        while True:
            m = _SENTENCE_END.match(self._speech_buf)
            if not m:
                break
            sentence = m.group(0)
            self._speech_buf = self._speech_buf[m.end():]
            self._speak_capped(sentence)

    def _flush_speech(self) -> None:
        if self._speech_buf.strip():
            self._speak_capped(self._speech_buf)
        self._speech_buf = ""

    def _speak_capped(self, raw: str) -> None:
        """Speak a cleaned sentence, up to the spoken-length cap. Past the cap, say the rest is
        on screen (once) and stop — so a long/verbose reply doesn't get narrated in full."""
        if self._speak_done:
            return
        s = _speakable(raw)
        if not s:
            return
        if re.sub(r"[^\w\s]", "", s).strip().lower() in _DEAD_REPLIES:
            print(f"[otto] not speaking dead reply: {s!r}", file=sys.stderr, flush=True)
            return
        cap = self.settings.voice.tts.max_spoken_chars or 0
        if cap and self._spoken_chars + len(s) > cap:
            remaining = cap - self._spoken_chars
            if remaining > 40:                       # squeeze in the start of this sentence
                head = s[:remaining].rsplit(" ", 1)[0].strip()
                if head:
                    self._voice_out(head + "…")
            self._voice_out("The rest is on screen.")
            self._speak_done = True
            return
        self._voice_out(s)
        self._spoken_chars += len(s) + 1

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
        self._voice_out(text)

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

    def set_mood(self, level: str, note: str = "") -> None:
        """Otto's explicit override of the dashboard's ambient severity color."""
        lvl = (level or "info").strip().lower()
        if lvl not in ("good", "info", "warn", "critical"):
            lvl = "info"
        self.bus.publish({"type": "mood", "level": lvl, "note": (note or "").strip(),
                          "source": "otto"})

    # -- per-workspace routing (called by each workspace's tools via WorkspaceContext) -----
    # Every workspace streams into its OWN chat window (events tagged with the project); only
    # the focused workspace is spoken aloud.
    async def _ws_say(self, project: str, text: str) -> None:
        ws = self.workspaces.get(project)
        if ws is not None:
            ws.last_reply = text
        self.bus.transcript("assistant", text, final=True, ws=project)
        if project == self.focused:
            self._voice_out(text)

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

    async def _ws_remember_fact(self, project: str, fact: str, scope: str = "project") -> bool:
        """Explicit 'remember that…' → durable fact in this project's (or the global) memory."""
        try:
            return await self.memory.add_fact(project, fact, scope)
        except Exception:
            return False

    async def _ws_remember_document(self, project: str, title: str, content: str) -> str:
        """Explicit 'remember this doc' → save it into the project's memory documents."""
        try:
            e = await self.memory.ingest_doc(project, title, content)
            return f"saved “{e['title']}” to this project's memory ({e['chars']} chars)"
        except Exception as ex:
            return f"couldn't save that note: {ex}"

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
