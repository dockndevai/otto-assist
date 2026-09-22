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
# NB: laya is prompt-fragile (a broadened variant of this once inverted accuracy to 36%) — keep
# each instruction NARROW and change it only with a fresh eval.
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

# "Is this a real, actionable request?" — a local (offline) stand-in for the cloud verify call,
# to drop grammatical-but-nonsensical STT hallucinations on the hands-free path.
_REALCMD = {
    "real": {
        "type": "noul",
        "instructions": (
            "Is this a coherent, actionable command, question, or request that a voice assistant "
            "could carry out, as opposed to garbled/nonsensical speech-to-text noise or a "
            "sentence fragment that asks for nothing?"
        ),
    }
}

# "Would this destroy data?" — a cheap safety pre-check before running under permission=bypass.
_DESTRUCTIVE = {
    "destructive": {
        "type": "noul",
        "instructions": (
            "Does this instruction ask to PERMANENTLY delete, erase, overwrite, reset, drop, "
            "force-push, or otherwise IRREVERSIBLY destroy files, data, branches, or history? "
            "Answer yes ONLY for clearly destructive or irreversible actions — not for reading, "
            "listing, searching, creating, or ordinary edits."
        ),
    }
}

# "Is the new request part of the current topic?" — the body carries CURRENT TOPIC + NEW REQUEST.
_CONTINUATION = {
    "continuation": {
        "type": "noul",
        "instructions": (
            "Given the CURRENT TOPIC and the NEW REQUEST below, is the new request a continuation "
            "of or closely related to the current topic (same task or subject), as opposed to a "
            "brand-new, unrelated task or a change of subject?"
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

    def _noul(self, body: str, question: dict, key: str) -> float | None:
        """Run one typed yes/no decision and return its probability in [0, 1], or None if the
        model isn't ready/available (caller then falls back / skips the check)."""
        if not self._ready or self._router is None:
            return None
        try:
            res = self._router.predict({"body": body}, question)
            return float(res["answers"][key]["noul"])
        except Exception:
            return None

    def score(self, text: str) -> float | None:
        """P(addressed to Orrin) in [0, 1] — or None if the model isn't ready/available (skip gate)."""
        return self._noul((text or "").strip(), _QUESTION, "addressed")

    def is_real_command(self, text: str) -> float | None:
        """P(coherent, actionable request) — local stand-in for the cloud hallucination verify."""
        return self._noul((text or "").strip(), _REALCMD, "real")

    def is_destructive(self, text: str) -> float | None:
        """P(destructive/irreversible) — a safety pre-check for permission='bypass'."""
        return self._noul((text or "").strip(), _DESTRUCTIVE, "destructive")

    def is_continuation(self, text: str, topic: str) -> float | None:
        """P(new request continues the current topic). `topic` is a short summary of the current
        chat/thread; a low score means it's an unrelated task (→ start a fresh context)."""
        body = f"CURRENT TOPIC: {(topic or '').strip()}\nNEW REQUEST: {(text or '').strip()}"
        return self._noul(body, _CONTINUATION, "continuation")
