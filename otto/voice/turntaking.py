"""Turn-taking / dialogue-state gate — decide whether a heard utterance is a COMPLETE turn to act
on, an UNFINISHED thought to keep listening for, a CORRECTION to fold in, or a request to RECOVER a
lost thought.

This replaces "commit the turn whenever silence > N ms" with a semantic decision. It runs between the
STT final transcript and command dispatch (`Orchestrator._dispatch_voice_command`). It is
deliberately pragmatic and deterministic (no model, no streaming partials needed): it catches the
*detectable* cases — trailing fillers/conjunctions, explicit hold phrases, corrections, and recovery
— and otherwise dispatches. (Sub-clause pause detection on an otherwise-complete phrase needs prosody
/ streaming partials, which arrive with the native audio front-end; this gate does not depend on
them.)
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# "what was I saying" — pull the held/last thought back.
_RE_RECOVER = re.compile(
    r"\b(what was i saying|where was i|what did i (just )?say|my train of thought|remind me what)\b", re.I)

# Bare hold phrases that mean "don't act yet, I'm still thinking". ("wait" alone is handled upstream
# as a stop-word, so it's intentionally omitted here.) Includes a few Hinglish forms.
_RE_HOLD = re.compile(
    r"^(?:one\s*sec(?:ond)?|hang on|hold on|give me (?:a\s+)?(?:sec|second|minute|moment)|"
    r"just a (?:sec|second|moment)|let me think|bear with me|um+|uh+|hmm+|er+|"
    r"ek\s*(?:minute|second|sec|minat)|zara ruko|ruko|ruk)\.?$", re.I)

# Resets that throw away the held thought and wait for the redo.
_RE_RESET = re.compile(r"\b(scratch that|forget (?:that|it)|never\s*mind(?: that)?|"
                       r"ignore (?:that|what i said)|cancel that|strike that)\b", re.I)

# Correction openers (the rest of the utterance amends the held thought).
_RE_CORRECTION = re.compile(r"^(?:actually|wait,?\s*actually|no,?\s*(?:i meant|wait|sorry)|"
                            r"hmm,?\s*actually|on second thought|let me rephrase|sorry,?\s*i meant)\b", re.I)

# Words that, when an utterance ENDS on them, signal it isn't finished.
_TRAILING_UNFINISHED = {
    "and", "or", "but", "so", "because", "cause", "cos", "then", "to", "the", "a", "an", "of",
    "for", "with", "that", "which", "while", "when", "if", "about", "into", "like", "maybe",
    "um", "uh", "er", "hmm", "i", "we", "you", "it's", "its", "is", "are", "my", "your", "think",
    "guess", "mean", "wanna", "gonna", "trying", "where", "what", "how",
}


@dataclass
class TurnDecision:
    kind: str          # "dispatch" | "hold" | "recover"
    text: str = ""     # dispatch → the full accumulated turn; recover → the thought to speak back


def _normalize(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip())


def looks_unfinished(text: str) -> bool:
    """True if `text` reads like a thought the speaker hasn't finished."""
    t = _normalize(text)
    if not t:
        return True
    if t.endswith(("…", "...", ",", "-", "–", "—")):
        return True
    if t.endswith((".", "?", "!")):
        return False
    words = re.findall(r"[\w']+", t.lower())
    if not words:
        return True
    if words[-1] in _TRAILING_UNFINISHED:
        return True
    # trailing filler pair like "i think" / "you know" / "sort of"
    if len(words) >= 2 and " ".join(words[-2:]) in {
            "i think", "i guess", "you know", "sort of", "kind of", "i mean", "i was", "we were",
            "let me", "i'll just", "i need to", "i want to", "trying to"}:
        return True
    return False


class TurnTaker:
    """Holds the in-progress spoken turn and decides when it is complete."""

    def __init__(self, voice_cfg=None) -> None:
        self.cfg = voice_cfg
        self._buffer = ""        # accumulated unfinished fragments
        self._last = ""          # last fully-dispatched turn (for recovery)

    @property
    def holding(self) -> bool:
        return bool(self._buffer)

    def preview(self) -> str:
        return self._buffer

    def offer(self, text: str) -> TurnDecision:
        """Classify `text` given what's already buffered."""
        t = _normalize(text)
        if not t:
            return TurnDecision("hold")

        # Recover a lost thought.
        if _RE_RECOVER.search(t):
            thought = (self._buffer or self._last).strip()
            return TurnDecision("recover", thought)

        # Reset: drop the held thought, keep listening for the redo.
        if _RE_RESET.search(t) and len(re.findall(r"[\w']+", t)) <= 4:
            self._buffer = ""
            return TurnDecision("hold")

        # Bare hold phrase ("one second", "um") — wait without adding it as content.
        if _RE_HOLD.match(t):
            return TurnDecision("hold")

        is_correction = bool(_RE_CORRECTION.match(t))
        combined = (self._buffer + " " + t).strip() if self._buffer else t
        combined = _normalize(combined)

        # Unfinished → accumulate and keep listening. A correction that is itself unfinished also
        # holds; a complete correction falls through to dispatch (folding the amend into the turn).
        if looks_unfinished(t) or (is_correction and looks_unfinished(t)):
            self._buffer = combined
            return TurnDecision("hold")

        # Complete → dispatch the whole accumulated turn.
        self._buffer = ""
        self._last = combined
        return TurnDecision("dispatch", combined)

    def flush(self) -> str:
        """Continuation window expired — return whatever is buffered (and clear it)."""
        out = self._buffer.strip()
        self._buffer = ""
        if out:
            self._last = out
        return out

    def reset(self) -> None:
        self._buffer = ""
