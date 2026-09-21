"""Fast local "is this speech actually for Orrin?" gate — makes hands-free always-listen usable.

In always-listen there's no wake word, so Orrin hears the whole room: side conversations, the
TV, people talking to each other, its own echo. The keyword heuristics (noise.py) catch obvious
junk but not grammatical ambient speech. This gate adds a small local model (laya — a
non-autoregressive typed-decision model, ~50 ms on CPU, no API cost, multilingual) that scores
how likely an utterance is DIRECTED AT the assistant (a command/question/request) vs ambient
talk, so we only act on speech meant for Orrin.

Loads the model once in a background thread; until it's ready (or if laya isn't installed) the
gate is transparent — score() returns None and the caller falls back to the keyword heuristics.
It never gates obvious commands (those bypass it upstream), only ambiguous input.
"""

from __future__ import annotations

import sys
import threading

# Broad on purpose: a command OR a question OR a request addressed to the assistant should pass;
# only speech clearly not directed at it (other people, media, thinking aloud) should score low.
_QUESTION = {
    "addressed": {
        "type": "noul",
        "instructions": (
            "Is the speaker giving a direct command or request to a voice assistant to DO "
            "something (open/play/set/remind/tell/close/search/answer), as opposed to ambient "
            "conversation or chit-chat not directed at the assistant?"
        ),
    }
}


class SmartGate:
    """Lazy, thread-safe wrapper around laya's Router for the addressed/ambient decision."""

    def __init__(self) -> None:
        self._router = None
        self._ready = False
        self._failed = False

    def start(self) -> None:
        """Load the model in the background so the first real command isn't blocked ~40s."""
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self) -> None:
        try:
            from laya import Router
            router = Router(preload=False)
            # Warm the checkpoints we'll actually use (English + multilingual for Hinglish) so
            # the first live scoring is ~50 ms, not a cold load.
            for warm in ("open the calculator", "gaana chalao"):
                try:
                    router.predict({"body": warm}, _QUESTION)
                except Exception:
                    pass
            self._router = router
            self._ready = True
            print("[otto] smart-gate (laya) ready", file=sys.stderr, flush=True)
        except Exception as e:
            self._failed = True
            print(f"[otto] smart-gate unavailable ({e!r}) — falling back to keyword heuristics",
                  file=sys.stderr, flush=True)

    @property
    def ready(self) -> bool:
        return self._ready

    def score(self, text: str) -> float | None:
        """P(addressed to Orrin) in [0, 1] — or None if the model isn't ready/available (skip gate)."""
        if not self._ready or self._router is None:
            return None
        try:
            res = self._router.predict({"body": (text or "").strip()}, _QUESTION)
            return float(res["answers"]["addressed"]["noul"])
        except Exception:
            return None
