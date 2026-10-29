"""Short spoken acknowledgements for the instant a voice command lands — so Orrin feels responsive
without dead air while the real reply is still coming.

Chosen by **task type** (lookup / debug / code / long_task) and kept natural:
- concise for voice (every phrase is under 8 words),
- never promising a duration or claiming progress that hasn't happened yet,
- avoiding the last few phrases so it never sounds like a loop,
- occasionally echoing the user's own request ("I'll trace where that flows.").

The orchestrator only speaks one when the request is actually going to the engine (a turn with
latency); instant local actions / memory answers skip it and begin immediately.
"""
from __future__ import annotations

import random
import re
from collections import deque

# Task types.
LOOKUP = "lookup"
DEBUG = "debug"
CODE = "code"
LONG = "long_task"

# Generic phrases per task type. Warm, professional, under 8 words, no duration promises.
_PHRASES: dict[str, list[str]] = {
    LOOKUP: ["Sure.", "On it.", "Yep, checking.", "Okay, one sec.", "Let me check."],
    DEBUG: ["I'm taking a look.", "Let me dig in.", "Looking into it now.", "On it — tracing that."],
    CODE: ["I'm looking through the code now.", "I'll trace where that flows.",
           "Checking the code now.", "On it — reading through it."],
    LONG: ["I'm digging into it.", "On it — I'll keep you posted.",
           "Getting into it now.", "Starting on that now."],
}

# Request-aware echoes: a keyword in the request → a phrase that reflects it back. Used occasionally.
_REQUEST_AWARE: list[tuple[re.Pattern, list[str]]] = [
    (re.compile(r"\b(trace|flow|path|pipeline|route)\b", re.I),
     ["I'll trace where that flows.", "I'm following the data flow now.", "I'll trace that path."]),
    (re.compile(r"\b(compare|versus|\bvs\b|difference|trade[- ]?offs?|which (is|one)|options?)\b", re.I),
     ["I'll compare the two approaches.", "I'll weigh the options."]),
    (re.compile(r"\b(latest|recent|last|current)\b.*\b(change|commit|diff|edit|update|version)\b", re.I),
     ["I'm checking the latest change.", "I'll look at the recent change."]),
    (re.compile(r"\b(why|failing|fails|broken|error|bug|crash|wrong|not working)\b", re.I),
     ["I'm tracing what's going wrong.", "Let me see what's happening."]),
    (re.compile(r"\b(where|find|locate|look for|search for)\b", re.I),
     ["I'll find where that lives.", "Let me locate that."]),
]

# Task-type classification cues.
_LONG_RE = re.compile(
    r"\b(redesign|rewrite|migrate|audit|everything|end[- ]to[- ]end|across all|whole (app|project|"
    r"codebase|thing)|go through all|review (all|the whole|the entire)|refactor the (whole|entire))\b",
    re.I)
_DEBUG_RE = re.compile(
    r"\b(why|debug|fix|failing|fails|broken|error|bug|crash|wrong|issue|problem|"
    r"not working|investigate|diagnose)\b", re.I)
_CODE_RE = re.compile(
    r"\b(code|function|class|method|module|file|refactor|implement|import|variable|"
    r"repo|branch|commit|diff|test|pipeline|trace|flow|where .*(defined|called|flows?|lives?))\b", re.I)


def classify_task(text: str, intent: str = "command") -> str:
    """Infer the task type used to pick ack wording, from the request text (+ the dispatch intent)."""
    t = text or ""
    if _LONG_RE.search(t):
        return LONG
    if _DEBUG_RE.search(t):
        return DEBUG
    if _CODE_RE.search(t):
        return CODE
    return LOOKUP            # short questions / plain lookups


class AckSelector:
    """Stateful picker: varies wording by task type, avoids the last few phrases, and occasionally
    reflects the user's own request back. Inject `rng` for deterministic tests."""

    def __init__(self, history: int = 3, request_aware_rate: float = 0.35,
                 rng: "random.Random | None" = None) -> None:
        self._recent: deque[str] = deque(maxlen=max(1, history))
        self._rate = max(0.0, min(1.0, request_aware_rate))
        self._rng = rng or random

    def _request_aware(self, text: str) -> list[str]:
        for rx, phrases in _REQUEST_AWARE:
            if rx.search(text or ""):
                return phrases
        return []

    def choose(self, task_type: str = LOOKUP, request_text: str = "") -> str:
        """Return a short acknowledgement for this task type, not one of the recent few."""
        pool = _PHRASES.get(task_type, _PHRASES[LOOKUP])
        aware = self._request_aware(request_text)
        if aware and self._rng.random() < self._rate:
            pool = aware
        fresh = [p for p in pool if p not in self._recent] or list(pool)
        phrase = self._rng.choice(fresh)
        self._recent.append(phrase)
        return phrase
