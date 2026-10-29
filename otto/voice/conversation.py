"""A conversational turn manager for voice.

Ties the dialogue pieces into one cohesive unit:
- **Topic / context** across turns, so a bare follow-up ("and the second one?") stays on-topic.
- **Fast vs long** task shaping (lookup / debug / code / long_task) via `acks.classify_task`.
- **Varied, contextual acknowledgements** via `AckSelector` (never the same phrase twice in a row).
- **Finished-speaking gate + self-correction**, delegated to the existing `TurnTaker` (hold until the
  thought is complete; fold in "actually, I meant…"; recover a lost thought) — it does NOT act until
  the user has actually finished.
- **Interruption** state, so a barge-in drops the held thought.
- **Concise voice replies** by default (first couple of sentences, capped), while the screen keeps
  the full text.
- **Real progress milestones** from tool activity ("Reading auth.py", "Running the tests") rather
  than generic filler.

This composes the existing engines (`TurnTaker`, `AckSelector`) — it does not reimplement them.
"""
from __future__ import annotations

import re

from .acks import LONG, AckSelector, classify_task
from .turntaking import TurnTaker

_STOP = {
    "the", "and", "for", "that", "this", "with", "what", "how", "why", "you", "can", "could",
    "would", "should", "please", "about", "into", "from", "your", "mine", "they", "them", "then",
    "just", "like", "want", "need", "show", "tell", "give", "make", "let", "are", "was", "were",
    "will", "have", "has", "had", "does", "did", "get", "got", "out", "now", "one", "two", "all",
}

# A bare follow-up that leans on the current topic rather than restating it.
_FOLLOWUP_RE = re.compile(
    r"^(?:and|also|but|what about|how about|then|ok(?:ay)?(?:\s+(?:and|now|so))?|"
    r"(?:the\s+)?(?:next|second|third|other|last|first)\s+one|that one|it|this|that)\b", re.I)

# Tool → a short, concrete milestone. {d} is the (shortened) detail; a template needing {d} with no
# detail is suppressed (we never announce a vague milestone).
_TOOL_MILESTONE = {
    "Read": "Reading {d}", "Edit": "Editing {d}", "MultiEdit": "Editing {d}", "Write": "Writing {d}",
    "Bash": "Running {d}", "Grep": "Searching for {d}", "Glob": "Looking for {d}",
    "WebSearch": "Searching the web", "WebFetch": "Reading {d}", "Task": "Digging into {d}",
}
# Tools whose activity is bookkeeping, not a milestone worth narrating.
_FILLER_TOOL = re.compile(r"^(?:TodoWrite|TodoRead|Think|NotebookRead|set_status|set_mood)$", re.I)


class ConversationManager:
    def __init__(self, voice_cfg=None, *, ack_selector: AckSelector | None = None,
                 turn_taker: TurnTaker | None = None) -> None:
        self.cfg = voice_cfg
        self.acks = ack_selector or AckSelector()
        self.turns = turn_taker or TurnTaker(voice_cfg)
        self.topic = ""          # keyword summary of the current thread
        self.last_user = ""
        self.last_reply = ""
        self.interrupted = False

    # ---- finished-speaking gate + self-correction (delegates to TurnTaker) ----
    def offer(self, text: str):
        """dispatch | hold | recover — holds until the thought is complete, folds corrections in."""
        return self.turns.offer(text)

    def flush(self) -> str:
        return self.turns.flush()

    @property
    def holding(self) -> bool:
        return self.turns.holding

    def interrupt(self) -> None:
        """Barge-in / stop: abandon the half-formed thought and mark the turn interrupted."""
        self.interrupted = True
        self.turns.reset()

    def resume(self) -> None:
        self.interrupted = False

    # ---- task shaping ----
    def classify(self, text: str, intent: str = "command") -> str:
        return classify_task(text, intent)

    def is_long(self, text: str, intent: str = "command") -> bool:
        return classify_task(text, intent) == LONG

    def acknowledge(self, text: str, intent: str = "command") -> str:
        """A short, varied acknowledgement chosen by task type (and occasionally the request)."""
        return self.acks.choose(classify_task(text, intent), text)

    # ---- topic / follow-up context ----
    def note_user(self, text: str) -> None:
        self.last_user = text or ""
        kw = self._keywords(text)
        if kw:                   # a bare follow-up keeps the existing topic
            self.topic = kw

    def note_reply(self, text: str) -> None:
        self.last_reply = text or ""

    def is_follow_up(self, text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return False
        if _FOLLOWUP_RE.match(t):
            return True
        words = set(re.findall(r"[a-z0-9']+", t.lower()))
        topic_words = set(self.topic.split())
        return bool(len(words) <= 6 and topic_words and (topic_words & words))

    def contextualize(self, text: str) -> str:
        """Expand a bare follow-up with the current topic so downstream keeps context; leaves a
        self-contained utterance untouched."""
        t = (text or "").strip()
        if not t or not self.topic:
            return text
        shares = set(self.topic.split()) & set(re.findall(r"[a-z0-9']+", t.lower()))
        if self.is_follow_up(t) and not shares:
            return f"{t} (follow-up about {self.topic})"
        return text

    @staticmethod
    def _keywords(text: str, n: int = 5) -> str:
        words = re.findall(r"[a-z0-9']{3,}", (text or "").lower())
        kw = [w for w in words if w not in _STOP]
        return " ".join(kw[:n])

    # ---- concise voice replies ----
    @staticmethod
    def concise(reply: str, max_sentences: int = 2, max_chars: int = 240) -> str:
        """Trim a reply to a concise SPOKEN form (first couple of sentences, capped at a word
        boundary). The dashboard still shows the full text; this only shapes what's said aloud."""
        t = re.sub(r"\s+", " ", (reply or "").strip())
        if not t:
            return ""
        sents = re.split(r"(?<=[.!?])\s+", t)
        out = " ".join(sents[:max_sentences]).strip()
        if len(out) > max_chars:
            out = out[:max_chars].rsplit(" ", 1)[0].rstrip(",;:—- ") + "…"
        return out

    # ---- real progress milestones (not filler) ----
    @staticmethod
    def is_filler_tool(tool_name: str) -> bool:
        """True for bookkeeping tools whose activity isn't a milestone worth surfacing."""
        return bool(tool_name and _FILLER_TOOL.match(tool_name))

    def milestone(self, tool_name: str, detail: str = "") -> str | None:
        """A short, real milestone for a tool call, or None for filler / a vague no-target call."""
        if not tool_name or _FILLER_TOOL.match(tool_name):
            return None
        d = self._short(detail)
        tmpl = _TOOL_MILESTONE.get(tool_name)
        if tmpl:
            if "{d}" in tmpl and not d:
                return None                      # no concrete target → don't narrate a vague step
            return tmpl.format(d=d)
        return f"{tool_name}: {d}" if d else None

    @staticmethod
    def _short(s: str, n: int = 40) -> str:
        s = re.sub(r"\s+", " ", (s or "").strip())
        if len(s) <= n:
            return s
        return s[:n].rsplit(" ", 1)[0] + "…"
