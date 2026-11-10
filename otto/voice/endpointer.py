"""Semantic endpointing — the local analogue of OpenAI's *semantic VAD*.

Fixed-silence endpointing (end the turn after N ms of quiet, always) is what makes a cascaded
voice loop feel sluggish: a finished sentence waits the same N ms as a half-finished one. This
module decides the end-of-turn window from the *content* of the live partial transcript instead:

- a thought that reads **complete** ("open chrome", "what's the weather?") commits fast (short
  silence), so Orrin answers the instant you stop;
- a thought that reads **unfinished** (trailing "and" / "to the", a hold phrase, a mid-quantity
  "set a timer for twenty…") extends the window up to a cap, so Orrin never cuts you off.

It is pure and deterministic (no model, no audio) so it unit-tests cleanly and adds no latency. It
reuses `turntaking.looks_unfinished` for the unfinished side and a small terminal-completeness check
for the fast side; everything in between falls to a middle `base` window. The orchestrator feeds it
each partial and pushes the resulting target to the native helper's VAD (`{"cmd":"endsilence"}`);
`should_commit` also lets a pure-Python caller decide directly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .turntaking import _RE_CORRECTION, _RE_HOLD, looks_unfinished

# A partial that ends cleanly on terminal punctuation, OR is a short clearly-shaped directive /
# question, is treated as COMPLETE → commit fast. (Whisper usually punctuates a finished sentence.)
_RE_TERMINAL = re.compile(r"[.!?]['\"”’]?$")

# Imperative openers that make a short utterance a clear, self-contained command even without
# punctuation ("open chrome", "close the calculator", "lock the screen").
_RE_IMPERATIVE = re.compile(
    r"^(?:open|close|quit|launch|start|stop|play|pause|mute|unmute|lock|sleep|"
    r"show|hide|switch|go|take|set|turn|send|read|find|search|type|scroll|click|"
    r"screenshot|remind|cancel|delete|undo|redo|copy|paste|save)\b", re.I)

# A wh-/aux question opener ("what is…", "how do I…", "can you…").
_RE_QUESTION = re.compile(
    r"^(?:what|who|where|when|why|which|how|is|are|do|does|did|can|could|would|should|will)\b", re.I)

# Ends on a bare quantity ("for twenty", "chapter five", "at 3") — the speaker is very likely still
# going ("…twenty-five minutes"). A trailing unit word ("minutes", "dollars") is NOT mid-quantity.
_RE_TRAILING_NUMBER = re.compile(
    r"\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|"
    r"fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|"
    r"ninety|hundred|thousand|million)$", re.I)


def _words(t: str) -> list[str]:
    return re.findall(r"[\w']+", t.lower())


@dataclass(frozen=True)
class EndpointDecision:
    commit: bool              # True → the turn is over now (trailing silence met the target)
    target_silence_ms: int    # how long of a pause should end THIS turn, given its content
    reason: str               # short tag for logs/tests: complete | unfinished | quantity | neutral


class Endpointer:
    """Content-aware end-of-turn window.

    `base_ms` is the neutral window (ambiguous phrase). `min_ms` is the snappy window for a clearly
    complete thought. `max_ms` is the patient window for an unfinished one. All three are clamped so
    `min_ms <= base_ms <= max_ms` regardless of config.
    """

    def __init__(self, *, base_ms: int = 500, min_ms: int = 250, max_ms: int = 900) -> None:
        self.min_ms = max(0, int(min_ms))
        self.max_ms = max(self.min_ms, int(max_ms))
        self.base_ms = min(self.max_ms, max(self.min_ms, int(base_ms)))

    def target_silence_ms(self, partial: str) -> tuple[int, str]:
        """The end-silence window THIS partial warrants, with a reason tag."""
        t = re.sub(r"\s+", " ", (partial or "").strip())
        if not t:
            return self.max_ms, "unfinished"        # nothing yet → stay patient

        # Hold phrase ("one sec", "um", "let me think") or an unfinished correction opener → wait.
        if _RE_HOLD.match(t) or (_RE_CORRECTION.match(t) and looks_unfinished(t)):
            return self.max_ms, "unfinished"

        # Trailing conjunction / article / filler, or an obviously open clause → stay patient.
        if looks_unfinished(t):
            return self.max_ms, "unfinished"

        # Ends mid-quantity ("set a timer for twenty") → the number is probably still growing.
        w = _words(t)
        if w and _RE_TRAILING_NUMBER.search(w[-1]):
            return self.base_ms, "quantity"

        # Clearly complete: terminal punctuation, or a short imperative/question → commit fast.
        short = len(w) <= 10
        if _RE_TERMINAL.search(t) or (short and (_RE_IMPERATIVE.match(t) or _RE_QUESTION.match(t))):
            return self.min_ms, "complete"

        return self.base_ms, "neutral"

    def decide(self, partial: str, trailing_silence_ms: int) -> EndpointDecision:
        """Given the live partial and how long the speaker has been quiet, decide end-of-turn."""
        target, reason = self.target_silence_ms(partial)
        return EndpointDecision(
            commit=int(trailing_silence_ms) >= target,
            target_silence_ms=target,
            reason=reason,
        )

    def should_commit(self, partial: str, trailing_silence_ms: int) -> bool:
        return self.decide(partial, trailing_silence_ms).commit
