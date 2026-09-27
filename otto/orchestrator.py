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


from . import sessions
from .brain.base import Brain
from .config import OTTO_HOME, Settings, save_config
from .history import History
from .memory import Memory
from .server.events import EventBus
from .workspace import Workspace

_log = logging.getLogger("otto")

# A spoken-sentence boundary while STREAMING: text up to a terminator (. ! ?) that is FOLLOWED
# by whitespace, or a newline. Requiring whitespace after the terminator means a period inside a
# decimal (3.0) or a filename (security.md) is NOT a boundary, so the reply isn't chopped into
# gibberish ("30." / "0." / "md security."). A terminator at the buffer's end is left buffered
# (more may be coming, e.g. the "0" of "3.0") and spoken at _flush_speech when the turn ends.
_SENTENCE_END = re.compile(r"(.*?(?:[.!?]+(?=\s)|\n))", re.S)

# #64 streaming TTS: find the first sentence terminator that is followed by whitespace, so we can
# speak sentence 1 while the model still generates the rest. A decimal ("3.0") isn't a boundary
# (the "." isn't followed by whitespace).
_FIRST_SENT = re.compile(r"[.!?](?=\s)")


def _split_first_sentence(text: str, min_chars: int = 8) -> tuple[str, str]:
    """(first_sentence, remainder) once there is real text after the first sentence boundary;
    ("", text) if there's no boundary yet, nothing follows it yet, or the sentence is too short."""
    m = _FIRST_SENT.search(text)
    if not m:
        return "", text
    end = m.end()
    sent, rest = text[:end], text[end:]
    if not rest.strip():                 # boundary is at the very end — more may still come; wait
        return "", text
    if len(sent.strip()) < min_chars:
        return "", text
    return sent, rest.lstrip()


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
_GREETINGS = {"hey", "hi", "ok", "okay", "yo", "hello", "hay", "namaste", "namaskar", "arre"}

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


# Number word ↔ digit map for self-echo matching: TTS says "four", the mic often transcribes the
# echo as "4" (or the reverse), so the two must reconcile or the echo slips the guard (#56).
_NUM_WORDS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11",
    "twelve": "12", "thirteen": "13", "fourteen": "14", "fifteen": "15", "sixteen": "16",
    "seventeen": "17", "eighteen": "18", "nineteen": "19", "twenty": "20", "thirty": "30",
    "forty": "40", "fifty": "50", "sixty": "60", "seventy": "70", "eighty": "80",
    "ninety": "90", "hundred": "100", "thousand": "1000", "million": "1000000",
}


def _canon_nums(words: list[str]) -> list[str]:
    """Map number words to their digit form so 'four' and '4' compare equal (echo matching only)."""
    return [_NUM_WORDS.get(w, w) for w in words]


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
    # Small edit distance, but only when both words are long enough to be distinctive —
    # otherwise short fillers slip through (e.g. "to" is edit-distance 2 from "otto" and would
    # falsely trigger the wake word in "how TO check ...").
    if len(target) >= 4 and len(word) >= 4 and _levenshtein(word, target) <= 2:
        return True
    if len(target) >= 4 and _levenshtein(word, target) <= 1:
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
# Strict confirmation parsing (#47). A spoken/typed yes/no gates consequential tool calls, so a
# refusal must NEVER read as approval. Negations veto FIRST — "no, don't go ahead", "not ok",
# "no go" all contain an affirmative token but are clearly refusals. Only a whole answer that is
# an affirmative phrase (optionally led by a greeting/filler) approves; anything else is unclear.
_NEGATIONS = {"no", "nope", "nah", "not", "don't", "dont", "do not", "stop", "cancel", "wait",
              "never", "negative", "hold", "abort", "nevermind", "skip", "unsure"}
_CONFIRM_FILLER = {"um", "uh", "well", "so", "please", "otto", "hey", "yeah", "ok", "okay"}
_CONFIRM_YES = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "confirm", "confirmed",
                "affirmative", "go", "go ahead", "do it", "please do", "yes go ahead",
                "yeah go ahead", "sure go ahead", "go for it", "sounds good", "do that",
                "proceed", "yes please", "absolutely", "definitely"}


def _notify_clean(s: str, limit: int = 200) -> str:
    """Sanitise agent-controlled text before it reaches a native notification: drop control
    characters (they can't be shown and confuse osascript) and cap the length."""
    cleaned = "".join(c for c in (s or "") if c == " " or (ord(c) >= 32 and c not in "\x7f"))
    cleaned = cleaned.strip()
    return cleaned[:limit]


def _parse_confirmation(answer: str) -> str:
    """Classify a confirmation answer as 'yes' | 'no' | 'unclear'. Negation wins over any
    affirmative token so refusals are never approved; only a whole affirmative phrase approves."""
    low = (answer or "").strip().lower()
    # normalise punctuation to spaces, keep apostrophes (don't/can't)
    low = re.sub(r"[^a-z' ]+", " ", low)
    low = re.sub(r"\s+", " ", low).strip()
    if not low:
        return "unclear"
    tokens = low.split()
    if any(t in _NEGATIONS for t in tokens) or low in _NEGATIONS:
        return "no"
    # strip leading filler ("um", "ok so", "please") before matching the affirmative phrase
    core = list(tokens)
    while len(core) > 1 and core[0] in _CONFIRM_FILLER:
        core = core[1:]
    phrase = " ".join(core)
    if phrase in _CONFIRM_YES or low in _CONFIRM_YES:
        return "yes"
    return "unclear"


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
        self.scheduler = None   # proactive reminders/timers; created in start()
        self.smart_gate = None  # fast local "is this for Orrin?" gate for hands-free listen
        self.speaker = None     # "is this MY voice?" verifier for hands-free (set in start())
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
        self._first_spoken = False   # first sentence already streamed out this turn (#64)
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
        # One-shot startup briefing: greet + recap on app open, cancelled the moment you speak.
        self._briefing_handle = None
        self._briefing_done = False
        # True while Orrin has just asked the user a question and is awaiting the spoken answer —
        # so the next utterance is treated as the answer (not gated as a random command).
        self._expecting_answer = False
        # Idle background research: a re-arming timer that quietly researches one project at a
        # time when you're idle. Paused the instant you speak/type.
        self._idle_research_handle = None
        self._research_ws: Workspace | None = None   # dedicated, kept OUT of self.workspaces
        self._research_running = False
        self._research_cancelled = False
        self._git_ct_cache: dict[str, tuple[float, float]] = {}   # path -> (commit_time, cached_at)
        self._research_pass_i = 0     # counts research passes → rotates the objective (feature/risk)
        self._last_roadmap_day = ""   # "YYYY-MM-DD" of the last roadmap synthesis (once/day)
        # Parallel "meanwhile" dispatch: a dedicated same-project background workspace (own brain +
        # lock) so a second command can run alongside the in-flight focused turn instead of queuing.
        self._parallel_ws: Workspace | None = None   # dedicated, kept OUT of self.workspaces
        self._parallel_pending = 0                   # in-flight/queued parallel turns (close at 0)
        # Daily morning brief: a re-arming timer; fires at most once per calendar day at/after the
        # configured local hour, on the first quiet moment. Cancelled the instant you speak/type.
        self._morning_brief_handle = None
        self._last_brief_day = ""     # "YYYY-MM-DD" of the last brief, so it fires once a day
        self._last_wake = 0.0     # debounce timestamp for wake-word detections
        self._last_ack = ""     # last greeting spoken (to ignore its own echo)
        # Recently-spoken lines (text, monotonic ts) — so we can drop the mic re-hearing Otto's
        # own voice through the speaker in always-listen (timing-independent echo guard).
        self._recent_spoken: list[tuple[str, float]] = []
        # Push-to-talk: capture the next utterance as a command, no wake word needed.
        self._ptt_armed = False
        self._ptt_handle = None
        # Hands-free "sleep": in always-listen, saying "stop listening" pauses Otto until the
        # wake word wakes it again (then it's fully hands-free once more). The mic stays on so
        # it can still hear the wake word; everything else is ignored while asleep.
        self._sleeping = False
        self._last_greet_ts = 0.0     # last time a greeting was spoken (to debounce repeats)
        # True while we've paused the user's media (music/video) during a wake->listen window,
        # so we know to resume it afterwards.
        self._ducked = False
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
        # Remember what we just said so the mic re-hearing it (speaker echo) can be dropped even
        # after the timed mute window closes. Keep the last few, ~30s.
        now = time.monotonic()
        self._recent_spoken.append((t, now))
        self._recent_spoken = [(x, ts) for (x, ts) in self._recent_spoken
                               if now - ts < 30][-8:]
        # ~14 chars/sec for macOS `say`, + a cooldown for the speaker/room tail after it stops.
        secs = max(1.0, len(t) / 14.0) + 2.5
        self._mute_input_until = max(self._mute_input_until, time.monotonic() + secs)
        # Echo handling. With barge-in ACTIVE we keep the mic LISTENING during speech so you can
        # say "stop" to cut in — the stop-word check runs before the echo filter, and everything
        # else (including Orrin's own echo) is dropped by _output_suppressing. With barge-in
        # inactive we physically mute the mic during speech (no voice "stop" mid-sentence; use the
        # button) — the safe default on built-in speakers. `_barge_in_active` auto-enables barge-in
        # only on a headphone/external route where the mic can't hear the output (#56).
        if self.ears is not None and not self._barge_in_active():
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

    # -- media ducking: pause the user's music/video while listening after the wake word ------
    def _media_playpause(self) -> None:
        """Send the system play/pause media key — toggles whatever is playing (YouTube in a
        browser, Spotify, Music). Doesn't touch Orrin's own TTS (that's afplay, not a media
        session). Best-effort; a failure must never break the voice flow."""
        try:
            from pynput.keyboard import Controller, Key
            Controller().tap(Key.media_play_pause)
        except Exception as e:
            print(f"[otto] media key failed: {e!r}", file=sys.stderr, flush=True)

    def _duck_media(self) -> None:
        """Pause playing media so the mic hears only you during the wake->command window."""
        if self._ducked or not self.settings.voice.stt.duck_media_on_wake:
            return
        self._ducked = True
        print("[otto] ducking media (pause)", file=sys.stderr, flush=True)
        self._media_playpause()

    def _unduck_media(self) -> None:
        """Resume the media we paused on wake (only if we actually paused it)."""
        if not self._ducked:
            return
        self._ducked = False
        print("[otto] unducking media (resume)", file=sys.stderr, flush=True)
        self._media_playpause()

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

    def _casual_reply(self, text: str) -> tuple[str, str]:
        """Return (spoken reply, kind) for chit-chat. `kind` (greet/how/thanks/praise/bye/ack)
        lets the caller debounce repeated greetings."""
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
        return random.choice(self._CASUAL_REPLIES[key]), key

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

    def _barge_in_active(self) -> bool:
        """Should the mic stay LIVE while Orrin speaks (so voice "stop" cuts in mid-sentence)?
        True if barge-in is forced on, OR auto-barge-in is enabled AND the output route is
        acoustically safe (headphones/AirPods/external — the mic can't hear Orrin). On the
        built-in speakers this returns False, so we stay half-duplex and never loop (#56)."""
        stt = self.settings.voice.stt
        if stt.barge_in:
            return True
        if getattr(stt, "barge_in_auto", False):
            try:
                from . import audio
                return audio.output_is_external()
            except Exception:
                return False
        return False

    def _output_suppressing(self) -> bool:
        """True while Otto is speaking (or in the cooldown after) — drop mic input then. Anchored
        to the mouth's ACTUAL playback (plus a short tail via recently_active), so the suppression
        can't under-cover a Kokoro clip and let the tail echo through as a command (#56)."""
        if self.mouth is None:
            return False
        try:
            if self.mouth.recently_active(1.5):   # playing, or finished within 1.5s
                return True
        except Exception:
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

        # On first run (incl. after a reinstall), proactively request Screen Recording so
        # computer-use can see the screen to click/type without a mid-task block. No-op if
        # already granted; only nags on a genuinely fresh setup, not every launch.
        try:
            from . import preflight
            if (preflight.is_first_run()
                    and preflight.screen_recording_status() != "authorized"):
                preflight.request_screen_recording()
        except Exception:
            pass

        self._publish_project()
        self._publish_session()
        # Usable immediately; the full health check (incl. a real Claude test turn) runs in the
        # background and flips the dashboard's "Otto ready" panel to green when it passes. This
        # runs on every start(), so it re-validates on each restart too.
        self._status("idle", "Ready")
        self._loop.create_task(self.run_preflight(request=False))
        # Proactive startup briefing: after a short grace period with no input, greet + recap.
        self._schedule_startup_briefing()
        # Seed the cross-project registry (so the overview spans every project) + arm idle research.
        self._loop.create_task(self._seed_registry())
        self._arm_idle_research()
        self._arm_morning_brief()
        # Proactive reminders/timers — load any persisted from a prior run and arm them.
        try:
            from .scheduler import Scheduler
            self.scheduler = Scheduler(self, OTTO_HOME / "reminders.json")
            self.scheduler.start(self._loop)
        except Exception as e:
            print(f"[otto] scheduler init failed: {e!r}", file=sys.stderr, flush=True)
        # Local typed-decision model (laya): powers the hands-free smart-gate, the local
        # command-verify, and the auto-fresh-context / destructive-action guards. Loaded once in
        # the background if ANY of those features is on. Transparent no-op until ready.
        v = self.settings.voice
        laya_wanted = (getattr(v.stt, "laya_needed", False)
                       or getattr(v, "auto_fresh_context", False)
                       or getattr(v, "destructive_guard", False))
        if laya_wanted:
            try:
                from .voice.smartgate import SmartGate
                self.smart_gate = SmartGate()
                self.smart_gate.start()
            except Exception as e:
                print(f"[otto] smart-gate init failed: {e!r}", file=sys.stderr, flush=True)
        # Speaker verification ("is this MY voice?") — loads the x-vector model in the background
        # only when enabled AND a voiceprint exists (enroll first). Fail-open until ready.
        if getattr(v.stt, "speaker_verify", False):
            try:
                from .voice.speaker import SpeakerVerifier
                vp = v.stt.speaker_voiceprint or str(OTTO_HOME / "voiceprint.npy")
                sv = SpeakerVerifier(model_id=v.stt.speaker_model, voiceprint_path=vp)
                if sv.has_voiceprint:
                    sv.start()
                    self.speaker = sv
                    print("[otto] speaker verification enabled", file=sys.stderr, flush=True)
                else:
                    print("[otto] speaker_verify on but no voiceprint — run "
                          "`python -m otto.voice.enroll_speaker` (skipping voice-ID)",
                          file=sys.stderr, flush=True)
            except Exception as e:
                print(f"[otto] speaker verifier init failed: {e!r}", file=sys.stderr, flush=True)

    async def _seed_registry(self) -> None:
        """Register every discoverable project in memory so the cross-project overview covers
        them all, not only ones Orrin has already talked in. Cheap, one-shot, off the voice path."""
        try:
            from .projects import list_projects
            for p in list_projects(current=self.focused, pinned=self.settings.brain.projects):
                try:
                    from pathlib import Path
                    mt = Path(p.path).expanduser().stat().st_mtime
                except Exception:
                    mt = None
                self.memory.register_project(p.path, last_active=mt)
        except Exception as e:
            print(f"[otto] registry seed failed: {e!r}", file=sys.stderr, flush=True)
        # Age out suggestions nobody acted on, so the queue reflects what's still live.
        try:
            days = float(getattr(self.settings.memory, "suggestion_stale_days", 14.0))
            self.memory.expire_stale_suggestions(max_age_days=days)
        except Exception:
            pass

    def _schedule_startup_briefing(self) -> None:
        v = self.settings.voice
        if not v.startup_briefing or self._briefing_done or self._loop is None:
            return
        delay = max(2.0, float(v.startup_briefing_delay))
        self._briefing_handle = self._loop.call_later(
            delay, lambda: self._loop.create_task(self._startup_briefing()))

    def _cancel_briefing(self) -> None:
        """You spoke/typed within the grace window — skip the proactive briefing."""
        self._briefing_done = True
        if self._briefing_handle is not None:
            self._briefing_handle.cancel()
            self._briefing_handle = None

    _BRIEFING_PROMPT = (
        "SYSTEM: The app just opened and the user hasn't said anything yet. You are their "
        "SYSTEM-LEVEL assistant for the whole Mac — do NOT fixate on one project. Greet them by "
        "name if you know it, then give a SHORT spoken briefing (2-3 natural sentences) at the "
        "SYSTEM level: how things stand ACROSS their projects (use the cross-project overview "
        "below), anything notable or pending anywhere, and one concrete suggestion for what to do "
        "next. Do not single out one project unless it's clearly the most important thing right "
        "now. If there's little to report, a brief friendly greeting + offer to help is fine. "
        "Speak it — no lists, no markdown.")

    async def _startup_briefing(self) -> None:
        self._briefing_handle = None
        if self._briefing_done:
            return
        self._briefing_done = True
        # Don't talk over a phone driving remotely, or if voice output is muted.
        if self.voice_muted:
            return
        if self.remote_clients > 0 and self.settings.voice.tts.mute_when_remote:
            return
        try:
            pre = self.memory.overview() if self.settings.memory.cross_project else ""
            prompt = (self._BRIEFING_PROMPT + "\n\n" + pre) if pre else self._BRIEFING_PROMPT
            await self._run_turn(self.focused_ws, prompt, echo_user=False)
        except Exception as e:
            print(f"[otto] startup briefing failed: {e!r}", file=sys.stderr, flush=True)

    # -- daily morning brief ----------------------------------------------
    _MORNING_BRIEF_PROMPT = (
        "SYSTEM: Daily morning brief — the user just became active for the day and hasn't said "
        "anything yet. You are their SYSTEM-LEVEL assistant across the whole Mac. Using the "
        "cross-project overview and open suggestions below (gathered by overnight background "
        "research), give a SHORT spoken briefing (3-4 natural sentences): what changed / what's "
        "notable across their projects, then the 1-2 most useful things they could do today "
        "(lead with any high-severity risk). Be specific and grounded in the notes below — no "
        "generic pep talk. Speak it naturally: no lists, no markdown. If there's genuinely "
        "nothing to report, a brief good-morning is fine.")

    def _arm_morning_brief(self, *, delay: float = 300.0) -> None:
        """Re-arming daily timer: check every few minutes and fire the brief once per day once
        we're past the configured hour and the user is quiet."""
        v = self.settings.voice
        if not getattr(v, "morning_brief", True) or self._loop is None:
            return
        if self._morning_brief_handle is not None:
            self._morning_brief_handle.cancel()
        self._morning_brief_handle = self._loop.call_later(
            max(30.0, delay), lambda: self._loop.create_task(self._morning_brief_tick()))

    def _cancel_morning_brief(self) -> None:
        if self._morning_brief_handle is not None:
            self._morning_brief_handle.cancel()
            self._morning_brief_handle = None

    def _morning_brief_due(self) -> bool:
        v = self.settings.voice
        if not getattr(v, "morning_brief", True):
            return False
        lt = time.localtime()
        today = time.strftime("%Y-%m-%d", lt)
        if self._last_brief_day == today:
            return False                                   # already briefed today
        return lt.tm_hour >= int(getattr(v, "morning_brief_hour", 9))

    async def _morning_brief_tick(self) -> None:
        self._morning_brief_handle = None
        if not self._morning_brief_due():
            self._arm_morning_brief()
            return
        # Only speak into a quiet moment — never talk over a turn, research, or a remote phone.
        if (not self._idle_quiescent() or self.voice_muted
                or (self.remote_clients > 0 and self.settings.voice.tts.mute_when_remote)):
            self._arm_morning_brief()
            return
        self._last_brief_day = time.strftime("%Y-%m-%d")
        try:
            pre = self.memory.overview() if self.settings.memory.cross_project else ""
            roadmap = self.memory.read_roadmap() if self.settings.memory.cross_project else ""
            if roadmap:
                pre = (pre + "\n\n[Today's prioritized roadmap — lead the brief with its top 1-2 "
                       "items:]\n" + roadmap).strip()
            prompt = (self._MORNING_BRIEF_PROMPT + "\n\n" + pre) if pre else self._MORNING_BRIEF_PROMPT
            await self._run_turn(self.focused_ws, prompt, echo_user=False)
        except Exception as e:
            print(f"[otto] morning brief failed: {e!r}", file=sys.stderr, flush=True)
        finally:
            self._arm_morning_brief()                       # re-arm for tomorrow

    # -- idle background research -----------------------------------------
    _RESEARCH_PROMPT = (
        "SYSTEM: Background research pass on THIS project — the user is idle and NOT watching. "
        "Work SILENTLY and strictly READ-ONLY: you may read files, run read-only git/shell "
        "(status, log, diff), and search — but you must NOT edit files, run write/destructive "
        "commands, or commit (those are blocked anyway). Investigate ONE concrete thing that "
        "advances understanding of this project (recent changes, an open thread, a likely next "
        "step, or a risk), reach a single clear conclusion. Then save what you learned: durable "
        "FACTS via the `remember` tool (1-3), and anything the user might ACT on via the "
        "`suggest` tool — one concrete actionable suggestion, plus any RISK you spotted "
        "(kind='risk', severity 'high' only for urgent hazards like data loss, security, or a "
        "broken build/release). Do not speak. Stop after saving.")

    _FEATURE_PROMPT = (
        "SYSTEM: Background PLANNING pass on THIS project — the user is idle and NOT watching. "
        "Work SILENTLY and strictly READ-ONLY (read files, read-only git/shell, search; no edits, "
        "no commits). Your job this pass is to think like a product+eng planner: propose ONE "
        "concrete NEW feature or improvement for this project, grounded in what the code actually "
        "is and where it's heading (recent commits, TODOs, gaps, rough edges). Sketch a short plan "
        "for it: WHAT it is (one line), WHY it helps, a ROUGH approach (2-4 steps / key files), and "
        "rough EFFORT (S/M/L). Then save it via the `suggest` tool as kind='action' with the text "
        "starting 'FEATURE: <name> — <why>; plan: <steps>; effort: <S|M|L>'. Optionally save 1-2 "
        "durable FACTS via `remember` if you learned something structural. Prefer a genuinely NEW "
        "idea over restating known work. Do not speak. Stop after saving.")

    _ROADMAP_PROMPT = (
        "SYSTEM: Daily cross-project ROADMAP synthesis — the user is idle and NOT watching. Using "
        "ONLY the cross-project overview and open suggestions below (gathered by background "
        "research), synthesize a single PRIORITIZED plan across ALL projects. Output a clean ranked "
        "list of the 5-8 highest-value things to do next: each item = `[project] what to do — why "
        "(one line) — effort S/M/L`. Put urgent risks (security, data loss, broken release) at the "
        "top, then high-leverage features/next-steps. Be concrete and specific; group nothing you "
        "can't ground in the notes below. Reply with ONLY the ranked list (no preamble). Do not use "
        "any tools. Do not speak.")

    def _research_prompt(self, path: str) -> str:
        """Base prompt + what we already know about this project, so the pass advances instead of
        re-deriving the same finding (finding-level dedup — project.md was accumulating near-
        duplicates because the brain was never told its own prior conclusions). Also lists the
        open suggestions so the pass can retire any that are already resolved. Rotates the
        objective: every Nth pass is feature/planning, the rest are investigate/risk."""
        every = int(getattr(self.settings.voice, "idle_research_feature_every", 3) or 0)
        is_feature = every > 0 and (self._research_pass_i % every == every - 1)
        base = self._FEATURE_PROMPT if is_feature else self._RESEARCH_PROMPT
        extra: list[str] = []
        try:
            known = self.memory.recent_findings(path, limit=12)
        except Exception:
            known = []
        if known:
            block = "\n".join(f"- {k}" for k in known)
            extra.append(
                "You have ALREADY saved these findings about this project in past passes — do "
                "NOT repeat, rephrase, or re-derive any of them. Investigate something genuinely "
                "NEW, or meaningfully advance one of these (e.g. it changed, was resolved, or a "
                "next step is now clear). If after looking you find nothing new worth saving, "
                "save nothing and stop.\nAlready known:\n" + block)
        try:
            opensugg = self.memory.list_suggestions(project_path=path, status="open", limit=12)
        except Exception:
            opensugg = []
        if opensugg:
            block = "\n".join(f"- (id {s['id']}) {s['text']}" for s in opensugg)
            extra.append(
                "These suggestions are already OPEN for this project — do NOT duplicate them. If "
                "your read shows any of them is now done or no longer applies, call "
                "`resolve_suggestion` with its id (status 'done' or 'dismissed').\n"
                "Open suggestions:\n" + block)
        if not extra:
            return base
        return base + "\n\n" + "\n\n".join(extra)

    def _arm_idle_research(self) -> None:
        v = self.settings.voice
        if not getattr(v, "idle_research", False) or self._loop is None:
            return
        if self._idle_research_handle is not None:
            self._idle_research_handle.cancel()
        delay = max(30.0, float(v.idle_research_delay))
        self._idle_research_handle = self._loop.call_later(
            delay, lambda: self._loop.create_task(self._idle_research_tick()))

    def _cancel_idle_research(self) -> None:
        """You spoke/typed — stop any pending or running background research immediately."""
        if self._idle_research_handle is not None:
            self._idle_research_handle.cancel()
            self._idle_research_handle = None
        if self._research_running:
            self._research_cancelled = True
            ws = self._research_ws
            if ws is not None and self._loop is not None:
                self._loop.create_task(ws.brain.interrupt())

    def _idle_quiescent(self) -> bool:
        return (self.state == "idle"
                and not self._sleeping
                and not self._awaiting_command
                and not self._follow_up_active
                and not (self._pending_answer is not None and not self._pending_answer.done())
                and self.focused_ws.state == "idle"
                and not self._research_running)

    def _research_quiescent(self) -> bool:
        """Like _idle_quiescent, but background research is SILENT so it may also run while asleep
        (paused via 'stop listening') when idle_research_while_asleep is on. Spoken things
        (morning brief) still use _idle_quiescent so they never talk over a sleep."""
        sleeping_ok = getattr(self.settings.voice, "idle_research_while_asleep", True)
        return (self.state == "idle"
                and (sleeping_ok or not self._sleeping)
                and not self._awaiting_command
                and not self._follow_up_active
                and not (self._pending_answer is not None and not self._pending_answer.done())
                and self.focused_ws.state == "idle"
                and not self._research_running)

    def _git_last_commit(self, path: str) -> float:
        """Unix time of the project's last git commit, 0 if not a repo. Cached briefly so a
        selection pass doesn't re-spawn git for every project every few minutes."""
        now = time.time()
        cached = self._git_ct_cache.get(path)
        if cached is not None and (now - cached[1]) < 600.0:   # 10-min TTL
            return cached[0]
        ct = 0.0
        try:
            out = subprocess.run(
                ["git", "-C", path, "log", "-1", "--format=%ct"],
                capture_output=True, text=True, timeout=3.0)
            if out.returncode == 0 and out.stdout.strip():
                ct = float(out.stdout.strip())
        except Exception:
            ct = 0.0
        self._git_ct_cache[path] = (ct, now)
        return ct

    def _next_research_project(self) -> str | None:
        """Pick the next project to research: rotate through everything that's due (past
        idle_research_min_gap), least-recently-researched first, but — when
        idle_research_git_weight is on — bias toward projects with recent git activity so
        attention tracks where the user is actually working."""
        from pathlib import Path
        from .memory import project_slug
        from .projects import list_projects, discover_recent
        try:
            cands = [p.path for p in list_projects(current=self.focused,
                                                   pinned=self.settings.brain.projects)]
        except Exception:
            cands = []
        if not cands:
            try:
                cands = list(discover_recent())
            except Exception:
                cands = []
        reg = self.memory.registry()
        now = time.time()
        gap = float(self.settings.voice.idle_research_min_gap)
        git_weight = bool(getattr(self.settings.voice, "idle_research_git_weight", True))
        eligible: list[tuple[float, float, str]] = []      # (git_recency, -last_research, path)
        home = str(Path.home())
        for path in cands:
            full = str(Path(path).expanduser())
            if full == str(Path(self.focused).expanduser()):
                continue                                   # never research the focused project
            if full in ("/", home) or len(full) < 4:       # skip junk "projects" (root, home)
                continue
            slug = project_slug(path)
            last = (reg.get(slug, {}) or {}).get("last_research") \
                or self.memory.project_updated_at(path)
            if last and (now - last) < gap:
                continue                                   # freshly researched — skip for now
            git_ct = self._git_last_commit(full) if git_weight else 0.0
            eligible.append((git_ct, -(last or 0.0), path))
        if not eligible:
            return None
        # Recent git activity first, then least-recently-researched (rotation/fairness fallback).
        eligible.sort(key=lambda x: (x[0], x[1]), reverse=True)
        mx = int(self.settings.voice.idle_research_max_projects or 0)
        pool = eligible[:mx] if mx else eligible
        return pool[0][2]

    def _roadmap_due(self) -> bool:
        """Once per calendar day, at/after the brief hour: time to re-synthesize the roadmap."""
        v = self.settings.voice
        if not getattr(v, "roadmap_synthesis", True) or not self.settings.memory.cross_project:
            return False
        lt = time.localtime()
        if self._last_roadmap_day == time.strftime("%Y-%m-%d", lt):
            return False
        return lt.tm_hour >= int(getattr(v, "morning_brief_hour", 9))

    async def _idle_research_tick(self) -> None:
        self._idle_research_handle = None
        if not self.settings.voice.idle_research or not self._research_quiescent():
            self._arm_idle_research()
            return
        # Once a day, synthesize the cross-project roadmap before the next project pass.
        if self._roadmap_due():
            await self._run_roadmap_synthesis()
            return                                  # re-arms in its own finally
        path = self._next_research_project()
        if path is None:
            self._arm_idle_research()
            return
        self._research_pass_i += 1                  # rotates feature vs investigate objective
        await self._run_idle_research(path)

    async def _run_roadmap_synthesis(self) -> None:
        """Daily: fold the whole cross-project overview + open suggestions into one prioritized
        plan and save it to memory/roadmap.md (silent; the morning brief reads it out)."""
        self._research_running = True
        self._research_cancelled = False
        self._last_roadmap_day = time.strftime("%Y-%m-%d")
        try:
            overview = self.memory.overview() if self.settings.memory.cross_project else ""
            if not overview:
                return
            ws = await self._open_research_ws(self.focused)
            if ws is None or self._research_cancelled:
                return
            print("[otto] roadmap synthesis (cross-project)", file=sys.stderr, flush=True)
            self.bus.publish({"type": "research", "active": True, "name": "roadmap"})
            await self._run_turn(ws, self._ROADMAP_PROMPT + "\n\n" + overview,
                                 echo_user=False, research=True)
            plan = (getattr(ws, "last_reply", "") or "").strip()
            if plan and not self._research_cancelled:
                self.memory.save_roadmap(plan)
                self.bus.publish({"type": "roadmap", "text": plan[:600]})
        except Exception as e:
            print(f"[otto] roadmap synthesis failed: {e!r}", file=sys.stderr, flush=True)
        finally:
            self.bus.publish({"type": "research", "active": False})
            await self._close_research_ws()
            self._research_running = False
            self._arm_idle_research()

    async def _run_idle_research(self, path: str) -> None:
        self._research_running = True
        self._research_cancelled = False
        try:
            self.memory.register_project(path)
            ws = await self._open_research_ws(path)
            # NB: don't re-check _idle_quiescent() here — we've already set _research_running,
            # which would make it False. Only bail if the user spoke/typed during connect.
            if ws is None or self._research_cancelled:
                return
            print(f"[otto] idle research -> {ws.name} (read-only)", file=sys.stderr, flush=True)
            self.bus.publish({"type": "research", "active": True, "name": ws.name})
            await self._run_turn(ws, self._research_prompt(path), echo_user=False, research=True)
            if not self._research_cancelled:
                self.memory.mark_researched(path)
        except Exception as e:
            print(f"[otto] idle research failed: {e!r}", file=sys.stderr, flush=True)
        finally:
            self.bus.publish({"type": "research", "active": False})
            await self._close_research_ws()
            self._research_running = False
            self._arm_idle_research()                      # cycle to the next project

    async def _open_research_ws(self, path: str) -> Workspace | None:
        from pathlib import Path
        p = str(Path(path).expanduser())
        if self._research_ws is not None and self._research_ws.project == p:
            return self._research_ws
        await self._close_research_ws()
        try:
            ws = Workspace(self, self.settings, p, new_chat=True, read_only=True)
            await ws.brain.connect()
            self._research_ws = ws
            return ws
        except Exception as e:
            print(f"[otto] research ws open failed: {e!r}", file=sys.stderr, flush=True)
            return None

    async def _close_research_ws(self) -> None:
        ws, self._research_ws = self._research_ws, None
        if ws is not None:
            try:
                await ws.brain.disconnect()
            except Exception:
                pass

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
                on_health=self._on_stt_health,
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
            preflight.Check("screen_recording", "Screen Recording (computer control)",
                            required=False, pane="screen_recording"),
        ]
        self._publish_preflight(done=False)
        # On an explicit recheck, (re-)fire the Screen Recording prompt so the user can grant
        # the computer-use permission in the same pass as the others.
        if request:
            try:
                preflight.request_screen_recording()
            except Exception:
                pass
        # NB: we deliberately do NOT call AVFoundation requestAccess (see start()); the recorder
        # opening the mic is what triggers the real prompt. `request` is kept for API compat.

        def _set(c) -> None:
            for i, x in enumerate(self.preflight_checks):
                if x.key == c.key:
                    self.preflight_checks[i] = c
                    break
            self._publish_preflight(done=False)

        # Run each check defensively: a diagnostic that itself throws (e.g. a headless/CI box
        # missing a subsystem) must NOT abort the whole preflight — the check keeps its
        # placeholder key/status and the rest still run.
        def _safe_set(fn) -> None:
            try:
                _set(fn(self.settings))
            except Exception as e:
                _log.warning("preflight check %s failed: %s", getattr(fn, "__name__", fn), e)
                self._publish_preflight(done=False)
        _safe_set(preflight.check_microphone)
        _safe_set(preflight.check_claude_cli)
        _safe_set(preflight.check_voice_output)
        _safe_set(preflight.check_accessibility)
        _safe_set(preflight.check_screen_recording)
        try:
            _set(await preflight.check_claude_test(self.settings))   # the slow one, last
        except Exception as e:
            _log.warning("preflight check_claude_test failed: %s", e)
            self._publish_preflight(done=False)

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
        self._cancel_idle_research()
        if self.scheduler is not None:
            self.scheduler.shutdown()
        await self._close_research_ws()
        await self._close_parallel_ws()
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
    # Project switching by voice: "switch to the auth project", "go to billing workspace",
    # "switch project to auth", "open the auth project".
    _RE_SWITCH_PROJECT = re.compile(
        r"^(?:switch|change|go|move|open)\s+(?:to\s+)?(?:the\s+)?(?P<name>.+?)"
        r"\s+(?:project|workspace|repo)\.?$", re.I)
    _RE_SWITCH_PROJECT2 = re.compile(
        r"^(?:switch|change)\s+(?:the\s+)?(?:project|workspace)\s+to\s+(?P<name>.+?)\.?$", re.I)
    # "in <name>, <command>" / "in the <name> workspace: <command>" — target another workspace.
    _RE_TARGET = re.compile(
        r"^in\s+(?:the\s+)?(?P<name>.+?)(?:\s+workspace)?\s*[,:]\s*(?P<cmd>.+)$", re.I)
    # Parallel-intent leads: "meanwhile, do X" — run X concurrently with the in-flight turn.
    # Three anchored forms so a normal sentence starting with "while…" isn't misread as parallel.
    _RE_MEANWHILE_SIMPLE = re.compile(
        r"^\s*(?:meanwhile|in\s+the\s+meantime|at\s+the\s+same\s+time|"
        r"in\s+parallel|simultaneously|in\s+the\s+background)\b[,:]?\s+(?P<cmd>.+)$", re.I)
    _RE_MEANWHILE_WHILE = re.compile(
        r"^\s*(?:while|whilst)\s+(?:that|this|it|the\s+\w+(?:\s+\w+)?)(?:'s|’s)?\s+"
        r"(?:runs?|ran|running|builds?|building|compil\w+|install\w+|download\w+|"
        r"process\w*|is\s+\w+ing|are\s+\w+ing|happens?|happening|goes?|going|"
        r"works?|working)\b[,:]?\s+(?P<cmd>.+)$", re.I)
    # "by the time <clause>, <command>" — comma splits the clause from the command (most reliable).
    _RE_MEANWHILE_BYTIME = re.compile(
        r"^\s*by\s+the\s+time\s+[^,]+,\s*(?P<cmd>.+)$", re.I)
    # No-comma fallback: end the clause at a progress verb followed by a request lead, so we don't
    # grab a verb mid-clause ("…this build is happening can you open Finder" → "open Finder").
    _RE_MEANWHILE_BYTIME2 = re.compile(
        r"^\s*by\s+the\s+time\s+.+?\b"
        r"(?:runs?|running|builds?|building|is\s+\w+ing|are\s+\w+ing|happens?|happening|"
        r"finish\w*|complet\w*|done|installs?|installing)\s+"
        r"(?=can\s+you|could\s+you|would\s+you|will\s+you|please|also)(?P<cmd>.+)$", re.I)
    _RE_MEANWHILE_LEAD = re.compile(
        r"^(?:can\s+you|could\s+you|would\s+you|will\s+you|please|also|and|then)\b\s*", re.I)
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
        m = self._RE_SWITCH_PROJECT.match(text) or self._RE_SWITCH_PROJECT2.match(text)
        if m:
            self.bus.transcript("user", text, final=True)
            self._loop.create_task(self._switch_project_by_name(m.group("name").strip()))
            return True
        m = self._RE_SWITCH_CHAT.match(text)
        if m:
            self.bus.transcript("user", text, final=True)
            self._loop.create_task(self._switch_chat_by_name(m.group("name").strip()))
            return True
        return False

    async def _switch_project_by_name(self, name: str) -> None:
        """Resolve a spoken project/workspace name to a path and switch focus to it."""
        path = self._resolve_workspace_name(name)
        if not path:
            await self.say(f"I couldn't find a project called “{name}”.")
            return
        from pathlib import Path
        await self.say(f"Switching to {Path(path).name}.")
        await self.switch_project(path)

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

    def _split_meanwhile(self, text: str) -> tuple[bool, str]:
        """Detect a parallel-intent lead ("meanwhile / while that runs / at the same time / by the
        time … , do X") and return (True, X) with the command stripped of the lead and any leading
        politeness ("can you", "please", "also"). Returns (False, "") for a normal utterance."""
        for rx in (self._RE_MEANWHILE_SIMPLE, self._RE_MEANWHILE_WHILE,
                   self._RE_MEANWHILE_BYTIME, self._RE_MEANWHILE_BYTIME2):
            m = rx.match(text)
            if not m:
                continue
            cmd = (m.group("cmd") or "").strip()
            # Strip up to two leading politeness/conjunction words ("can you", "also please …").
            for _ in range(2):
                stripped = self._RE_MEANWHILE_LEAD.sub("", cmd)
                if stripped == cmd:
                    break
                cmd = stripped.strip()
            if cmd:
                return True, cmd
        return False, ""

    async def _open_parallel_ws(self) -> Workspace | None:
        """A dedicated background workspace on the FOCUSED project (its own new-chat brain + lock)
        so a parallel turn runs alongside the in-flight focused turn without sharing its session
        or its dashboard lane. Reused across concurrent 'meanwhile' commands; closed when idle."""
        p = self.focused
        if self._parallel_ws is not None and self._parallel_ws.project == p:
            return self._parallel_ws
        await self._close_parallel_ws()
        try:
            ws = Workspace(self, self.settings, p, new_chat=True)
            ws.name = f"{ws.name} (parallel)"
            ws.tag = p + " ·parallel"     # own dashboard lane (never interleaves the focused chat)
            await ws.brain.connect()
            self._parallel_ws = ws
            return ws
        except Exception as e:
            print(f"[otto] parallel ws open failed: {e!r}", file=sys.stderr, flush=True)
            return None

    async def _close_parallel_ws(self) -> None:
        ws, self._parallel_ws = self._parallel_ws, None
        if ws is not None:
            try:
                await ws.brain.disconnect()
            except Exception:
                pass

    async def _run_parallel_turn(self, cmd: str, original: str) -> None:
        """Run `cmd` concurrently with the in-flight focused turn, in the parallel workspace.
        Silent (never spoken — the focused turn keeps the voice), streams to its own lane, and
        notifies on completion. Falls back to the normal queue if a parallel brain can't start."""
        self._parallel_pending += 1
        try:
            ws = await self._open_parallel_ws()
            if ws is None:
                await self._run_turn(self.focused_ws, original)   # couldn't parallelize → queue it
                return
            self.bus.transcript("user", cmd, final=True, ws=ws.tag)
            await self._run_turn(ws, cmd, echo_user=False, background=True)
        finally:
            self._parallel_pending -= 1
            if self._parallel_pending <= 0:
                await self._close_parallel_ws()

    # -- input routing -----------------------------------------------------
    def submit_user_input(self, text: str) -> None:
        """Entry point for both typed and spoken input."""
        text = (text or "").strip()
        if not text:
            return
        self._cancel_briefing()   # you gave a command — skip the proactive startup briefing
        self._cancel_idle_research()   # ...and pause any background research immediately
        if self._is_stop_command(text):   # typed/said "stop" — halt now
            self.interrupt_now("text")
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
        # "meanwhile / while that runs / at the same time, do X" — if a focused turn is already
        # running, run X CONCURRENTLY in a parallel same-project workspace instead of queuing it
        # behind the in-flight turn. If nothing's running there's nothing to parallelize, so just
        # run the (de-prefixed) command normally, now.
        if getattr(self.settings.voice, "parallel_dispatch", True):
            is_par, par_cmd = self._split_meanwhile(text)
            if is_par:
                if self.focused_ws.lock.locked():
                    self._loop.create_task(self._run_parallel_turn(par_cmd, text))
                else:
                    self._loop.create_task(self._handle_turn(par_cmd))
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

    # -- interruption / barge-in ------------------------------------------
    # Whole-utterance "stop" phrases that halt Orrin immediately. "stop listening" is NOT here —
    # that's the sleep command, handled separately.
    _STOP_WORDS = {
        "stop", "stop it", "stop stop", "stop stop stop", "stop talking", "stop please",
        "okay stop", "ok stop", "wait", "wait wait", "hold on", "hold on hold on", "cancel",
        "never mind", "nevermind", "quiet", "be quiet", "shush", "shut up", "enough", "pause",
        "halt", "abort", "stop now", "that's enough", "thats enough",
        # Hinglish (#i18n Tier 1)
        "ruko", "ruk jao", "ruk jaao", "rukiye", "band karo", "band karo isko", "bas", "bas karo",
        "chup", "chup ho jao", "rehne do", "rok do",
    }

    def _is_stop_command(self, text: str) -> bool:
        return " ".join(_norm_words(text)) in self._STOP_WORDS

    def interrupt_now(self, reason: str = "stop") -> None:
        """Halt whatever Orrin is doing right now — cut the current speech AND interrupt the
        running turn. Wired to the 'stop' word (heard in a gap), the dashboard Stop button, and
        the push-to-talk hotkey. Reliable regardless of the mic (button/hotkey don't need it)."""
        print(f"[otto] interrupt ({reason})", file=sys.stderr, flush=True)
        self._speak_done = True
        self._speech_buf = ""
        self._cancel_briefing()            # a Stop during the startup greeting halts it too
        self._cancel_idle_research()
        if self.mouth is not None:
            try:
                self.mouth.stop()          # cut the audio that's playing
            except Exception:
                pass
        self._mute_input_until = 0.0       # re-open the mic right away
        if self.ears is not None:
            try:
                self.ears.set_mic(True)
            except Exception:
                pass
        self._cancel_await()
        self._expecting_answer = False
        self._unduck_media()
        if self._loop is not None:         # interrupt the focused brain turn (best-effort)
            try:
                self._loop.create_task(self.focused_ws.brain.interrupt())
            except Exception:
                pass
        self._status("idle", "Stopped")

    def _on_voice_final(self, text: str) -> None:
        if self.mic_muted:
            return
        # Log every heard utterance (transcript-level captures), even ones we ignore below —
        # useful for reviewing mis-hears in always-listen.
        try:
            self.memory.record_capture(self.focused, text, source="voice")
        except Exception:
            pass
        # "stop" / "wait" / "cancel" — halt immediately, in any mode. (Heard whenever the mic is
        # on, i.e. in the gap after Orrin's speech or while it's working — during actual playback
        # the mic is muted, so use the Stop button or the hotkey for a hard mid-sentence cut.)
        if self._is_stop_command(text):
            self.interrupt_now("voice")
            return
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
            # Never let Orrin's OWN voice become the "command"/"answer": drop a greeting-ack echo,
            # anything still overlapping its speech, or a line matching what it just said. This is
            # what stopped the startup greeting from answering its own question in a loop.
            if (self._is_ack_echo(text) or self._output_suppressing()
                    or self._is_self_echo(text)):
                print(f"[otto] awaiting: ignoring self-echo: {text!r}",
                      file=sys.stderr, flush=True)
                return
            matched, remainder = self._match_wake(text)
            if matched and not remainder.strip():
                print(f"[otto] ignoring wake echo: {text!r}", file=sys.stderr, flush=True)
                return   # just the wake word again — keep waiting for the command
            command = remainder if (matched and remainder.strip()) else text
            hands_free = self._follow_up_active     # a follow-up window is hands-free; after a
            # If Orrin just asked a question, THIS utterance is the answer — don't run it through
            # the anti-hallucination gate (which was dropping real answers like "pass first").
            if self._expecting_answer:
                hands_free = False
                self._expecting_answer = False
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
        # Timing-independent echo guard: even after the mute window closes, the mic can catch
        # the tail of what Otto just said (esp. in always-listen on speakers). Drop anything
        # that closely matches a line Otto spoke in the last ~30s, so it never answers itself.
        if self._is_self_echo(text):
            print(f"[otto] ignoring self-echo (recent reply): {text!r}",
                  file=sys.stderr, flush=True)
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

    _GREET_DEBOUNCE = 30.0     # seconds — don't repeat a spoken greeting within this window

    # Utterances that put Otto to sleep. Short/ambiguous ones must match the whole (peeled)
    # utterance; the distinctive multi-word ones below may appear embedded in a longer sentence.
    _SLEEP_PHRASES = {
        "stop listening", "stop listning", "stop listen", "go to sleep", "goto sleep",
        "go to sleep now", "pause listening", "stand by", "standby", "mute yourself",
        "go quiet", "be quiet", "stop for now", "sleep", "sleep now", "take a break",
        "stop responding", "quiet down", "shush", "silence", "stop talking", "go away",
        # Hinglish (#i18n Tier 1)
        "so jao", "so jaao", "so jao", "chup ho jao", "aram karo", "sun na band karo",
        "sunna band karo", "abhi ke liye ruko",
    }
    # Distinctive enough to accept even when embedded ("okay otto, go to sleep now buddy").
    _STRONG_SLEEP = ("go to sleep", "goto sleep", "stop listening", "stop listning",
                     "go quiet", "be quiet", "mute yourself", "pause listening",
                     "stop responding", "stop talking", "go to sleep now",
                     "so jao", "so jaao", "chup ho jao", "sun na band karo")
    # Filler/politeness/address peeled from the ends before matching.
    _SLEEP_LEAD = _GREETINGS | {"so", "please", "just", "um", "uh", "well", "now", "can", "could",
                                "would", "will", "you", "lets", "let", "and", "otto", "orrin", "pls"}
    _SLEEP_TRAIL = {"please", "now", "okay", "ok", "thanks", "thank", "you", "otto", "orrin",
                    "buddy", "pls", "for", "a", "bit", "while", "please"}

    def _is_sleep_command(self, text: str) -> bool:
        words = _norm_words(text)
        if not words:
            return False
        # Don't misfire on a negated sentence ("I don't want to go to sleep").
        if any(w in ("dont", "not", "never", "cant", "cannot", "wont", "dnt") for w in words):
            return False
        # Check distinctive sleep phrases on the WHOLE utterance first — before peeling filler,
        # since some (e.g. "so jao") start with a word that's also a filler lead-in ("so").
        full = " ".join(words)
        if any(p in full for p in self._STRONG_SLEEP):
            return True
        trigger = (_norm_words(self.settings.voice.stt.wake_words)[-1:] or ["otto"])[0]
        while words and (words[0] in self._SLEEP_LEAD or _wordish(words[0], trigger)):
            words = words[1:]
        while words and (words[-1] in self._SLEEP_TRAIL or _wordish(words[-1], trigger)):
            words = words[:-1]
        core = " ".join(words)
        if not core:
            return False
        if core in self._SLEEP_PHRASES:
            return True
        return any(p in core for p in self._STRONG_SLEEP)

    def _enter_sleep(self) -> None:
        """Pause hands-free listening until the wake word. The mic stays on for the wake word."""
        self._sleeping = True
        self._cancel_await()
        self._unduck_media()   # resume any media we paused before going quiet
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
        # BUT a clearly command-shaped utterance ("open chrome", "close the calculator", "otto,
        # do X") is a real directive — trust it and skip the model gate, which was over-rejecting
        # genuine chained follow-ups (it dropped "close the calculator" as ambient). Keeps the gate
        # only for ambiguous/declarative input, so room chatter is still filtered.
        from .voice.noise import looks_like_command, looks_like_question
        # Hands-free semantic gate for ambiguous input. Obvious commands ("open chrome") AND clear
        # questions ("what's the weather", "otto …?") skip it and stay instant — the laya gate only
        # sees the ambiguous middle (declarative/ambient-shaped speech), where it drops room/TV/
        # side-talk. Prefer the fast LOCAL laya gate when ready; else the Haiku verify (if enabled).
        laya_ready = self.smart_gate is not None and self.smart_gate.ready
        smart_ready = bool(getattr(stt, "smart_gate", False) and laya_ready)
        # A follow-up window (right after Orrin replied) no longer BYPASSES the gate — that opened a
        # long, unfiltered window that ran the whole room as commands. Instead it RELAXES the
        # threshold in _dispatch_async, so a genuine conversational follow-up still passes while
        # clear ambient is still dropped.
        ambiguous = (hands_free and not deliberate
                     and not looks_like_command(text) and not looks_like_question(text))
        use_laya = ambiguous and smart_ready
        # If the addressing gate isn't active, still verify ambiguous hands-free input: locally via
        # laya when present (offline, ~50 ms), else the cloud Haiku verify (if enabled).
        local_verify = bool(ambiguous and not smart_ready and laya_ready
                            and getattr(stt, "local_command_verify", True))
        need_verify = ambiguous and not smart_ready and not local_verify and stt.hands_free_verify
        # Speaker verification runs on the whole hands-free path (even obvious commands — a colleague
        # can say "open chrome" too), before the content gate. Only when enrolled + model ready.
        spk_ready = bool(getattr(stt, "speaker_verify", False) and self.speaker is not None
                         and self.speaker.ready and self.speaker.has_voiceprint)
        need_speaker = spk_ready and hands_free and not deliberate
        need_gate = use_laya or local_verify or need_verify or need_speaker
        follow_up = bool(self._follow_up_active)
        # Intent first: react like a person, not a switch. Chit-chat ("hey", "how are you",
        # "thanks") gets a warm INSTANT reply right here — no Claude round-trip, no "On it." —
        # then a short hands-free window so you can keep talking. A question or an action flows
        # on to Claude with an ack that fits what you asked. Dictation types verbatim, so it
        # opts out of intent shaping.
        intent = "command"
        if not self._dictation:
            intent = self._classify_intent(text)
            if intent == "casual":
                reply, kind = self._casual_reply(text)
                # Debounce greetings: with always-listen, ambient "hello"s from the room made
                # Orrin greet over and over. If it already greeted in the last window, stay quietly
                # listening instead of greeting again (only greetings are debounced — real chit-chat
                # like "thanks"/"how are you" still gets a reply).
                now = time.monotonic()
                if kind == "greet" and (now - self._last_greet_ts) < self._GREET_DEBOUNCE:
                    print(f"[otto] suppressing repeat greeting: {text!r}",
                          file=sys.stderr, flush=True)
                    return
                if kind == "greet":
                    self._last_greet_ts = now
                print(f"[otto] casual: {text!r} -> {reply!r}", file=sys.stderr, flush=True)
                self.bus.transcript("user", text, final=True)
                self.bus.transcript("assistant", reply, final=True)
                self._status("idle", "Ready")
                self._voice_out(reply)
                # Keep the conversation open, hands-free — no need to say the wake word again.
                self._enter_awaiting_command(greet=False, follow_up=True)
                return
        if not stt.refine and not self._dictation and not need_gate:
            self._ack(intent); self.submit_user_input(text)   # fast path — ack, no model calls
            return
        if self._loop is not None:
            self._loop.create_task(self._dispatch_async(
                text, need_gate, intent, use_laya, local_verify, follow_up, need_speaker))
        else:
            self.submit_user_input(text)

    def _utterance_audio(self):
        """(float32 mono PCM, sample_rate) of the most recent finalized utterance, or (None, sr)."""
        ears = self.ears
        pcm = getattr(ears, "last_audio", None) if ears is not None else None
        if not pcm:
            return None, 16000
        from .voice.speaker import to_float32_mono
        return to_float32_mono(pcm), int(getattr(ears, "sample_rate", 16000))

    async def _dispatch_async(self, text: str, gate: bool = False,
                              intent: str = "command", use_laya: bool = False,
                              local_verify: bool = False, follow_up: bool = False,
                              need_speaker: bool = False) -> None:
        if gate:
            stt = self.settings.voice.stt
            # Speaker gate FIRST: if it isn't your voice, drop it whatever it says.
            if need_speaker and self.speaker is not None:
                audio, sr = self._utterance_audio()
                if audio is not None:
                    try:
                        sim = await self._loop.run_in_executor(
                            None, self.speaker.verify, audio, sr)
                    except Exception:
                        sim = None
                    thr = float(getattr(stt, "speaker_threshold", 0.75))
                    if sim is not None and sim < thr:
                        print(f"[otto] speaker mismatch (sim={sim:.2f}<{thr:.2f}) — not your "
                              f"voice: {text!r}", file=sys.stderr, flush=True)
                        return
            if use_laya and self.smart_gate is not None:
                try:
                    score = await self._loop.run_in_executor(None, self.smart_gate.score, text)
                except Exception:
                    score = None
                # In a follow-up window use the lower bar (drop only clear ambient); otherwise the
                # stricter addressing threshold.
                thr = float(getattr(stt, "follow_up_threshold", 0.30) if follow_up
                            else getattr(stt, "smart_gate_threshold", 0.55))
                if score is not None and score < thr:
                    print(f"[otto] smart-gate dropped ambient (p={score:.2f}<{thr:.2f}): {text!r}",
                          file=sys.stderr, flush=True)
                    return
            elif local_verify and self.smart_gate is not None:
                try:
                    p = await self._loop.run_in_executor(
                        None, self.smart_gate.is_real_command, text)
                except Exception:
                    p = None
                if p is not None and p < 0.30:      # conservative: drop only clear non-commands
                    print(f"[otto] local verify dropped (p={p:.2f}): {text!r}",
                          file=sys.stderr, flush=True)
                    return
            else:
                try:
                    from .voice.noise import is_real_command
                    if not await is_real_command(
                            text, model=self.settings.voice.stt.verify_model):
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
        # Trigger as the 1st or 2nd word. STT very often prepends or garbles the greeting
        # ("Hey Otto" -> "Here Otto" / "Hi Otto" / "A Otto", or just "Otto"), so accept the
        # trigger in either of the first two slots and take the rest as the command.
        for i in (0, 1):
            if i < len(norm) and _wordish(norm[i], trigger):
                return True, " ".join(norm[i + 1:])
        # fused whole phrase, e.g. "Hey Otto" -> "hiyoto" vs "heyotto"
        concat = "".join(phrase)
        if len(concat) >= 6 and _levenshtein(norm[0], concat) <= 2:
            return True, " ".join(norm[1:])
        # Wake word buried LATE in a very long capture. This happens when the mic picked up
        # continuous audio (a video/podcast on the speakers) and the user finally said
        # "…hey otto, do X" near the end — the whole blob became one utterance. Only for
        # abnormally long captures (never normal conversation, so "someone said hey otto
        # later" still won't trigger): take the command after the LAST wake occurrence.
        if len(norm) >= 30:
            last = -1
            for i, w in enumerate(norm):
                if _wordish(w, trigger) or (len(concat) >= 6 and _levenshtein(w, concat) <= 2):
                    last = i
            if 0 <= last < len(norm) - 1:
                return True, " ".join(norm[last + 1:])
        return False, ""

    def _enter_awaiting_command(self, greet: bool = True, timeout: float = 12,
                                follow_up: bool = False) -> None:
        """Wait for a command with no wake word. `greet` speaks a short ack; `follow_up` marks a
        post-reply window (hands-free, stricter noise gate). A wake-word activation is not a
        follow-up even when it's silent — the two are independent."""
        self._awaiting_command = True
        self._follow_up_active = follow_up
        # On an explicit wake (not a post-reply follow-up), pause any playing media so the mic
        # hears only the command; it resumes when the window ends (timeout/sleep) or the turn
        # finishes with nothing more to listen for.
        if not follow_up:
            self._duck_media()
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

    def _is_self_echo(self, text: str) -> bool:
        """Is `text` the mic re-hearing something Otto just SAID (speaker echo)? Compares the
        heard words against Otto's recently-spoken lines: a contained phrase or >70% word
        overlap counts as an echo. Timing-independent, so it catches the tail that slips past
        the mute window."""
        tw = _canon_nums(_norm_words(text))
        if not tw:
            return False
        tset, tjoined = set(tw), " ".join(tw)
        now = time.monotonic()
        for spoken, ts in self._recent_spoken:
            if now - ts > 30:
                continue
            sw = _canon_nums(_norm_words(spoken))
            if not sw:
                continue
            if tjoined and tjoined in " ".join(sw):        # heard is a chunk of what we said
                return True
            if len(tset & set(sw)) / len(tset) > 0.7:      # strong overlap with a spoken line
                return True
        return False

    def _await_timeout(self) -> None:
        if self._awaiting_command:
            self._awaiting_command = False
            self._follow_up_active = False
            self._expecting_answer = False
            self._unduck_media()   # no command came — resume whatever we paused on wake
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
        self._cancel_briefing()   # you started talking — skip the proactive startup briefing
        # NB: do NOT cancel idle research here on mere voice-activity — ambient/room noise trips
        # this constantly and would keep research from ever running. A REAL command cancels it
        # (submit_user_input), and it's a silent background workspace anyway; the quiescence gate
        # keeps new research from starting while a focused turn is active.
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

    def _on_stt_health(self, status: str) -> None:
        """The recorder went deaf at runtime (mic permission revoked / device unplugged) or
        recovered. Tell the user — going silently deaf with no feedback is the worst failure."""
        if status == "deaf":
            self._status("error", "Can't hear the microphone")
            self.notify("Orrin can't hear you",
                        "The microphone stopped working — check Privacy & Security ▸ Microphone, "
                        "or reconnect your mic.")
            self._voice_out("I've lost the microphone — please check its permission, "
                            "or reconnect it.")
        elif status == "ok":
            self.notify("Microphone back", "Orrin can hear you again.")
            self._voice_out("Okay, I can hear you again.")
            if self.state.startswith("error"):
                self._status("idle", "Ready")

    def _on_ptt_press(self) -> None:
        # Global hotkey (⌘⇧J): first HALT whatever Orrin's doing (barge-in — reliable, no mic
        # needed), then arm capture of your next utterance — same as the Talk button.
        self.interrupt_now("hotkey")
        self.arm_ptt()

    def _on_ptt_release(self) -> None:
        pass  # VAD detects end-of-speech; nothing to do on release

    # -- the turn ----------------------------------------------------------
    async def _handle_turn(self, text: str, *, echo_user: bool = True) -> None:
        """Run a turn in the focused workspace. First applies two local (laya) safety/hygiene
        checks: a destructive-action confirm (even under bypass) and auto-fresh-context on a clear
        topic change. Both degrade to a no-op when laya isn't ready."""
        ws = self.focused_ws
        if not await self._destructive_ok(text):
            return                                   # user declined a destructive-looking command
        await self._maybe_fresh_context(ws, text)    # topic change → new chat before the turn
        ws.last_command = text
        await self._run_turn(ws, text, echo_user=echo_user)

    async def _destructive_ok(self, text: str) -> bool:
        """Under permission='bypass', ask laya whether a command looks destructive/irreversible and,
        if so, require a spoken confirm. Returns True to proceed, False if the user declined."""
        v = self.settings.voice
        if not (getattr(v, "destructive_guard", False) and self.smart_gate is not None
                and self.smart_gate.ready
                and self.settings.brain.permission == "bypass" and self._loop is not None):
            return True
        try:
            p = await self._loop.run_in_executor(None, self.smart_gate.is_destructive, text)
        except Exception:
            p = None
        if p is None or p < 0.55:
            return True
        print(f"[otto] destructive guard (p={p:.2f}) — confirming: {text!r}",
              file=sys.stderr, flush=True)
        ok = await self.confirm(f"That sounds destructive — {text}. Should I go ahead?")
        if not ok:
            self.bus.transcript("assistant", "Okay, I won't do that.", final=True)
            self._voice_out("Okay, I won't do that.")
        return ok

    async def _maybe_fresh_context(self, ws: Workspace, text: str) -> None:
        """If this command clearly starts a NEW topic (laya says it doesn't continue the current
        one), rebind the focused workspace to a fresh chat so a stale thread can't bleed into the
        answer. Durable memory is re-injected on the new chat's first turn."""
        v = self.settings.voice
        if not (getattr(v, "auto_fresh_context", False) and self.smart_gate is not None
                and self.smart_gate.ready and self._loop is not None
                and getattr(ws, "last_command", "") and ws.session_id):
            return
        try:
            cont = await self._loop.run_in_executor(
                None, self.smart_gate.is_continuation, text, ws.last_command)
        except Exception:
            cont = None
        if cont is not None and cont < 0.35:
            print(f"[otto] topic change (cont={cont:.2f}) — fresh context for: {text!r}",
                  file=sys.stderr, flush=True)
            try:
                await ws.rebind(self.settings, new_chat=True)
            except Exception as e:
                print(f"[otto] fresh-context rebind failed: {e!r}", file=sys.stderr, flush=True)

    async def _run_turn(self, ws: Workspace, text: str, *, echo_user: bool = True,
                        research: bool = False, background: bool = False) -> None:
        """Run one turn for a workspace. Different workspaces run concurrently (per-workspace
        lock). EVERY workspace streams its real work — the user's turn, tool calls, and reply —
        to its OWN chat window in the dashboard (events tagged with the workspace). Only the
        *focused* workspace is spoken aloud; the others work in the background and post a
        completion notification when done. This mirrors having several Claude windows open.

        `background=True` forces the silent, notify-on-done path even when this workspace is on
        the focused project — used for parallel "meanwhile" turns so they run alongside (never
        speak over) the in-flight focused turn and never clobber its chat session."""
        async with ws.lock:
            p = ws.project
            tag = getattr(ws, "tag", p)   # dashboard window id (parallel turns get their own lane)
            focused = p == self.focused and not background
            print(f"[otto] turn -> {ws.name} (focused={focused}): {text!r}",
                  file=sys.stderr, flush=True)
            if echo_user:
                self.bus.transcript("user", text, final=True, ws=tag)   # show it in this window
            if focused:
                self._status("thinking", "Thinking…")
                self._speech_buf = ""
                self._spoken_chars = 0
                self._speak_done = False
                self._first_spoken = False
            ws.state = "thinking"
            self._publish_workspaces()
            assistant_text = ""
            errored = False   # so a voice-only user hears that a turn failed (not just a toast)
            # Prepend durable memory (facts + recent summary + relevant saved-note excerpts)
            # so Claude has cross-session context. The dashboard/transcript already showed the
            # user's *clean* text above; only what we send to the brain carries the
            # (clearly-delimited) preamble. Optimization: within one resumed chat the model
            # already retains the earlier preamble, so we only (re)inject on the first turn or
            # when the remembered content has actually changed — avoiding redundant tokens every
            # turn. Doc-retrieval excerpts vary by turn, so those still refresh naturally.
            try:
                pre = self.memory.preamble(p, query=text)
                # System-level stance: give Orrin the whole-system picture by default — inject the
                # cross-project digest at the START of a session (first turn) and on any
                # "across my projects / what should we do" question. The _last_preamble dedup
                # keeps it from repeating every turn, so this stays bounded.
                if (self.settings.memory.cross_project and not research
                        and (Memory.is_cross_project_query(text)
                             or getattr(ws, "_last_preamble", None) is None)):
                    ov = self.memory.overview()
                    if ov:
                        pre = (ov + "\n\n" + pre) if pre else ov
                if pre and pre != getattr(ws, "_last_preamble", None):
                    send_text = pre + text
                    ws._last_preamble = pre
                else:
                    send_text = text
            except Exception:
                send_text = text
            try:
                async for ev in ws.brain.send(send_text):
                    live = p == self.focused and not background   # focus can change mid-turn
                    if ev["type"] == "delta":
                        chunk = ev["text"]
                        assistant_text += chunk
                        self.bus.assistant_delta(chunk, ws=tag)   # into this window
                        if live:
                            self._feed_speech(chunk)              # ...but only speak the focused
                            self._status("speaking", "Speaking…")
                        ws.state = "speaking"
                    elif ev["type"] == "tool_use":
                        ws.state = "working"
                        # The tool call itself IS the visible work — show it in the window
                        # (that's the "chat window" feel). No spoken narration of tools.
                        self._announce_tool(ev["name"], ev.get("detail", ""), ws=tag)
                        if live:
                            self._status("working", ev["name"])
                    elif ev["type"] == "result":
                        if ev.get("is_error"):
                            errored = True
                            self.bus.notify(
                                "Brain error",
                                ev.get("error_text") or "The model returned an error.")
                        sid = ev.get("session_id")   # a new chat mints its id on turn 1
                        # Only persist when the chat id actually changed — it's stable after
                        # turn 1, so writing config.yaml every turn was needless disk + YAML
                        # work on the loop. NEVER persist a background/parallel chat's id: it's a
                        # throwaway new_chat on the focused project and would clobber the real
                        # chat's resume id.
                        if sid and not background \
                                and sid != getattr(ws, "_persisted_sid", None):
                            ws._persisted_sid = sid
                            try:
                                self._persist_session(p, sid)
                            except Exception:
                                pass
                            if p == self.focused:
                                self._publish_session()
            except Exception as e:
                errored = True
                print(f"[otto] turn error ({ws.name}): {e!r}", file=sys.stderr, flush=True)
                _log.exception("Turn failed in workspace %s", ws.name)
                self.bus.notify("Error", str(e))
                # A stream/decode error (e.g. a tool result that overflowed the SDK's JSON
                # buffer) corrupts the client — without a rebuild EVERY later turn returns
                # empty. Reconnect (resumes the same chat) so the brain recovers. Best-effort.
                try:
                    rc = getattr(ws.brain, "reconnect", None)
                    if rc is not None:
                        await rc()
                        print(f"[otto] brain reconnected after error ({ws.name})",
                              file=sys.stderr, flush=True)
                except Exception as re:
                    print(f"[otto] brain reconnect failed ({ws.name}): {re!r}",
                          file=sys.stderr, flush=True)
            finally:
                reply = assistant_text.strip()
                print(f"[otto] turn done ({ws.name}), reply len={len(reply)}",
                      file=sys.stderr, flush=True)
                # A voice-only user (screen not visible) must HEAR that a request failed —
                # otherwise a crashed turn is pure silence. Speak a short apology when the
                # focused, user-facing turn errored and produced no reply to speak.
                if errored and not reply and not research and not background \
                        and p == self.focused:
                    try:
                        self._voice_out("Sorry — that hit an error. Mind trying again?")
                    except Exception:
                        pass
                ws.state = "idle"
                ws.last_reply = reply
                if reply:
                    self.bus.transcript("assistant", reply, final=True, ws=tag)
                    # Learn from the exchange in the background (records the turn + folds any
                    # durable facts/summary into the store). Off the voice path, never blocks.
                    # Research turns save their own findings via the remember tool, so skip the
                    # extra capture pass (avoids a second model call + double-writing).
                    if self._loop is not None and not research:
                        self._loop.create_task(
                            self.memory.capture(p, text, reply, session_id=ws.session_id))
                if research:
                    # Idle research runs unattended — a quiet, throttled note, not the loud
                    # "finished" nudge, and only when it actually concluded something.
                    if reply and not self._research_cancelled:
                        self.notify(f"Researched {ws.name}",
                                    reply.split(chr(10), 1)[0][:140])
                elif focused:
                    self._flush_speech()
                    # Open a silent follow-up window so the next command needs no wake word —
                    # unless we're asleep, dictating, or awaiting an explicit answer. If Orrin
                    # ENDED WITH A QUESTION, always open a window (even if follow-ups are off) and
                    # mark that the next utterance is the answer — so "publish now or pass first?"
                    # actually gets your reply instead of dropping it.
                    fu = self.settings.voice.stt.follow_up_seconds
                    is_question = bool(reply) and reply.rstrip().endswith("?")
                    window = float(fu) if (fu and fu > 0) else (20.0 if is_question else 0.0)
                    if (window > 0 and reply and not self._sleeping
                            and not self._dictation
                            and not (self._pending_answer and not self._pending_answer.done())):
                        self._expecting_answer = is_question
                        self._enter_awaiting_command(greet=False, follow_up=True, timeout=window)
                    else:
                        self._unduck_media()   # nothing more to listen for — resume media
                        self._status("idle", "Ready")
                    self._arm_idle_research()   # back to idle → let background research resume
                else:
                    # Background window finished — nudge the user; its chat is there to switch to.
                    summary = reply.split("\n", 1)[0][:140] if reply else "Done."
                    self.notify(f"{ws.name} finished", summary)
                self._publish_workspaces()

    def _feed_speech(self, chunk: str) -> None:
        """Accumulate the reply's deltas. With `stream_sentences` on (#64) we speak the FIRST
        complete sentence as soon as it lands — so audio starts while the model is still
        generating — then the remainder is spoken as ONE clip at _flush_speech (not per-sentence,
        which synthesized choppy). Off = the old single-clip-at-end behavior."""
        if not self.mouth or self.voice_muted:
            return
        self._speech_buf += chunk
        tts = self.settings.voice.tts
        if (getattr(tts, "stream_sentences", True) and not self._first_spoken
                and not self._speak_done):
            sent, rest = _split_first_sentence(
                self._speech_buf, int(getattr(tts, "stream_min_chars", 12)))
            if sent:
                self._speech_buf = rest
                self._first_spoken = True
                self._emit_speech(sent)

    def _flush_speech(self) -> None:
        buf = self._speech_buf
        self._speech_buf = ""
        if buf.strip():
            self._emit_speech(buf)
        self._speak_done = True                        # turn's speech is complete

    def _emit_speech(self, raw: str) -> None:
        """Speak one cleaned chunk (first sentence, or the remainder), respecting the TOTAL
        spoken-length cap across the turn's clips. Skips a dead reply ('okay.'/'sure.') and, once
        the cap is hit, trims at a word boundary and stops the rest."""
        if self._speak_done:
            return
        s = _speakable(raw)
        if not s:
            return
        if re.sub(r"[^\w\s]", "", s).strip().lower() in _DEAD_REPLIES:
            print(f"[otto] not speaking dead reply: {s!r}", file=sys.stderr, flush=True)
            return
        cap = self.settings.voice.tts.max_spoken_chars or 0
        if cap:
            remaining = cap - self._spoken_chars
            if remaining <= 0:
                return                                 # already spoke the cap's worth this turn
            if len(s) > remaining:                     # runaway reply — trim at a word boundary
                s = s[:remaining].rsplit(" ", 1)[0].rstrip(" ,;:—-") + "…"
                self._speak_done = True                # nothing more to speak after the trim
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
        """Gate a consequential action on a spoken/typed yes/no. Parses strictly (a refusal is
        never read as approval), re-asks once when the answer is unclear, then denies. Every
        decision is emitted as an `approval_decided` event so it's auditable in history."""
        answer = await self.ask_user(f"I'm about to {action}. Should I go ahead?")
        decision = _parse_confirmation(answer)
        if decision == "unclear":
            answer2 = await self.ask_user(
                f"Sorry, I didn't catch a clear yes or no — should I {action}? Please say yes or no.")
            decision = _parse_confirmation(answer2)
            answer = f"{answer} | {answer2}"
            if decision == "unclear":
                decision = "no"   # unclear twice → deny (fail closed)
        approved = decision == "yes"
        self.bus.publish({"type": "approval_decided", "action": action,
                          "answer": answer, "decision": decision, "approved": approved})
        return approved

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

    async def _ws_suggest(self, project: str, text: str, kind: str = "action",
                          severity: str = "low", rationale: str = "") -> str:
        """Save an actionable suggestion / risk to the queue, and escalate genuinely-new
        high-severity risks (notification + dashboard alert) so they don't sit unseen."""
        if not getattr(self.settings.memory, "suggestions", True):
            return "suggestion queue is disabled"
        try:
            rec = self.memory.add_suggestion(project, text, kind=kind, severity=severity,
                                             rationale=rationale)
        except Exception as ex:
            return f"couldn't save that suggestion: {ex}"
        if rec is None:
            return "already had that suggestion open"
        # Dashboard tile for every new suggestion; a hard alert only for new high-severity risks.
        self.bus.publish({"type": "suggestion", "item": rec})
        if (rec.get("kind") == "risk" and rec.get("severity") == "high"
                and getattr(self.settings.memory, "risk_alerts", True)):
            self.bus.publish({"type": "alert", "item": rec})
            self.notify(f"⚠ Risk in {rec.get('name', 'a project')}", rec.get("text", ""))
        return f"saved suggestion (id {rec['id']})"

    async def _ws_resolve_suggestion(self, suggestion_id: str, status: str = "done") -> str:
        try:
            ok = self.memory.set_suggestion_status(suggestion_id, status)
        except Exception as ex:
            return f"couldn't update that suggestion: {ex}"
        if ok:
            self.bus.publish({"type": "suggestion_resolved", "id": suggestion_id,
                              "status": status})
            return f"marked {suggestion_id} {status}"
        return "no matching open suggestion"

    # -- reminders / timers (proactive scheduler) -------------------------
    @staticmethod
    def _fmt_when(secs: float) -> str:
        secs = int(max(0, secs))
        if secs < 60:
            return f"{secs}s"
        if secs < 3600:
            return f"{secs // 60}m" + (f" {secs % 60}s" if secs % 60 else "")
        h, rem = divmod(secs, 3600)
        return f"{h}h" + (f" {rem // 60}m" if rem // 60 else "")

    async def _ws_remind(self, message: str, delay_seconds: float,
                         repeat_seconds: float = 0.0) -> str:
        if self.scheduler is None:
            return "reminders aren't available right now"
        if not (message or "").strip():
            return "what should I remind you about?"
        try:
            rec = self.scheduler.add(float(delay_seconds), message,
                                     repeat_seconds=float(repeat_seconds or 0.0))
        except Exception as ex:
            return f"couldn't set that reminder: {ex}"
        self.bus.publish({"type": "reminder_set", "item": rec})
        when = self._fmt_when(float(delay_seconds))
        if rec.get("repeat"):
            return f"set — I'll remind you every {self._fmt_when(rec['repeat'])} (id {rec['id']})"
        return f"set — I'll remind you in {when} (id {rec['id']})"

    async def _ws_list_reminders(self) -> str:
        if self.scheduler is None:
            return "reminders aren't available right now"
        items = self.scheduler.active()
        if not items:
            return "no reminders set"
        now = time.time()
        lines = []
        for r in items:
            when = self._fmt_when(float(r.get("fire_at", now)) - now)
            rep = f" (every {self._fmt_when(r['repeat'])})" if r.get("repeat") else ""
            lines.append(f"(id {r['id']}) in {when}{rep}: {r.get('message', '')}")
        return "\n".join(lines)

    async def _ws_cancel_reminder(self, reminder_id: str) -> str:
        if self.scheduler is None:
            return "reminders aren't available right now"
        rec = self.scheduler.cancel(reminder_id)
        if rec is None:
            return "no matching reminder"
        self.bus.publish({"type": "reminder_cancelled", "id": rec["id"]})
        return f"cancelled: {rec.get('message', rec['id'])}"

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
        # Best-effort native macOS notification. title/body are agent-controlled (the `notify`
        # tool, the /mcp server, and _describe() output all flow here), so NEVER build the
        # AppleScript source from them — a trailing backslash in the body breaks out of the
        # string literal and turns the title into `do shell script …`. Pass them as argv items
        # so osascript treats them purely as data, and sanitise first (control chars, length).
        try:
            argv = [_notify_clean(body or ""), _notify_clean(title)]
            subprocess.Popen(
                ["osascript",
                 "-e", "on run argv",
                 "-e", "display notification (item 1 of argv) with title (item 2 of argv)",
                 "-e", "end run", "--", *argv]
            )
        except Exception:
            pass
