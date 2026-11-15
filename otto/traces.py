"""Turn traces — the small pure helpers around the `traces` table in otto/store.py.

Every turn Orrin handles leaves one row: which route answered it (the on-device router, a memory
command, or an engine), which engine, how long until the first words and until done, how it ended,
and — if the user says so — whether the answer was any good. Aggregated per query *bucket*, that is
enough to see where time goes and which engine does best at which kind of request.

This module only classifies text; the store owns the data and the orchestrator does the recording.
"""
from __future__ import annotations

import re
import statistics

_CODE = re.compile(
    r"\b(code|bug|fix|refactor|function|class|method|test|tests|pytest|compile|build|lint|deploy|"
    r"commit|branch|merge|rebase|git|repo|pull request|pr|diff|stack ?trace|exception|error|api|"
    r"endpoint|script|regex|sql|query|schema|migration|docker|config|install|dependency|package)\b",
    re.I)
_LOOKUP = re.compile(
    r"\b(email|emails|inbox|gmail|calendar|meeting|schedule|drive|document|doc|file|folder|search|"
    r"find|look up|weather|news|price|who is|what is|when is|where is|summar(y|ize|ise)|read)\b",
    re.I)

BUCKETS = ("code", "lookup", "short", "long", "general")


def classify(text: str) -> str:
    """A coarse query class for per-bucket stats: code / lookup / short / long / general."""
    t = (text or "").strip()
    if _CODE.search(t):
        return "code"
    if _LOOKUP.search(t):
        return "lookup"
    if len(t) < 40:
        return "short"
    if len(t) > 300:
        return "long"
    return "general"


_PFX = r"^(?:(?:hey|ok|okay)\s+)?(?:otto|orrin)?[\s,]*"
_END = r"[\s.!]*$"
# A pure rating: nothing for the engine to do, so Orrin just notes it.
_RATE_UP = re.compile(_PFX + r"(?:thumbs up|(?:that was |that's |this is )?(?:a )?(?:good|great|perfect|helpful) "
                      r"(?:answer|response|one)|that (?:was|is) (?:really |very )?helpful|that helped(?: a lot)?)"
                      + _END, re.I)
_RATE_DOWN = re.compile(_PFX + r"(?:thumbs down|(?:that was |that's |this is )?(?:a )?(?:bad|wrong|terrible|useless) "
                        r"(?:answer|response|one)|(?:that (?:was|is) )?not helpful|"
                        r"that (?:didn't|did not) help)" + _END, re.I)
# A correction: counts against the last answer, but the engine should still hear it and fix it.
_CORRECTION = re.compile(_PFX + r"(?:no[\s,]+)?that(?:'s| is| was) (?:wrong|incorrect|not right|not correct|"
                         r"not what i (?:asked|meant|wanted))\b", re.I)


def parse_feedback(text: str) -> tuple[float, bool] | None:
    """Is this utterance feedback on the previous answer? Returns (value, consume): value is 1.0
    (good) or 0.0 (bad); `consume` is True for a pure rating and False for a correction that should
    still reach the engine. None when it isn't feedback."""
    t = (text or "").strip()
    if not t or len(t) > 80:
        return None
    if _RATE_UP.match(t):
        return 1.0, True
    if _RATE_DOWN.match(t):
        return 0.0, True
    if _CORRECTION.match(t):
        return 0.0, False
    return None


def _median(vals: list[float]) -> float | None:
    vals = [v for v in vals if v is not None]
    return round(statistics.median(vals), 1) if vals else None


def summarize(rows: list[dict], *, min_samples: int = 5) -> dict:
    """Aggregate trace rows into per-(route, engine, bucket) stats, plus the engine that has done
    best in each bucket once at least `min_samples` engine turns back it.

    score = 0.6 * success-rate + 0.4 * mean feedback (0.5 when nobody rated), so reliability leads
    and explicit ratings break ties.
    """
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for r in rows:
        groups.setdefault((r.get("route") or "", r.get("engine") or "", r.get("bucket") or ""), []).append(r)
    stats: list[dict] = []
    for (route, engine, bucket), rs in sorted(groups.items()):
        ok = sum(1 for r in rs if r.get("outcome") == "ok")
        fb = [r["feedback"] for r in rs if r.get("feedback") is not None]
        success = ok / len(rs)
        feedback = (sum(fb) / len(fb)) if fb else None
        stats.append({
            "route": route, "engine": engine, "bucket": bucket, "n": len(rs),
            "success": round(success, 3),
            "feedback": round(feedback, 3) if feedback is not None else None, "rated": len(fb),
            "ttft_ms": _median([r.get("ttft_ms") for r in rs]),
            "total_ms": _median([r.get("total_ms") for r in rs]),
            "score": round(0.6 * success + 0.4 * (feedback if feedback is not None else 0.5), 3),
        })
    best: dict[str, dict] = {}
    for s in stats:
        if s["route"] != "engine" or s["n"] < min_samples:
            continue
        cur = best.get(s["bucket"])
        # higher score wins; on a tie the faster engine does
        if cur is None or (s["score"], -(s["total_ms"] or 0)) > (cur["score"], -(cur["total_ms"] or 0)):
            best[s["bucket"]] = {"engine": s["engine"], "score": s["score"], "n": s["n"],
                                 "total_ms": s["total_ms"]}
    routes: dict[str, int] = {}
    for r in rows:
        routes[r.get("route") or ""] = routes.get(r.get("route") or "", 0) + 1
    return {"total": len(rows), "routes": routes, "stats": stats, "best_engine": best}
