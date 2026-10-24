"""Latency harness — measure how fast the LOCAL action path is, so the win over routing everything
to Claude/Cursor (a full agent turn = seconds) is visible and tunable.

Run `python -m otto.latency`. It times representative local actions (in-process native queries and
subprocess calls) and prints p50/p95/min/max in milliseconds.
"""
from __future__ import annotations

import os
import statistics
import subprocess
import tempfile
import time
from typing import Callable


def measure(fn: Callable[[], object], n: int = 20) -> dict:
    samples: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            fn()
        except Exception:
            pass
        samples.append((time.perf_counter() - t0) * 1000.0)
    samples.sort()
    return {
        "n": n,
        "p50": round(statistics.median(samples), 2),
        "p95": round(samples[min(len(samples) - 1, int(len(samples) * 0.95))], 2),
        "min": round(samples[0], 2),
        "max": round(samples[-1], 2),
        "mean": round(statistics.mean(samples), 2),
    }


def _frontmost_app() -> object:
    from AppKit import NSWorkspace
    return NSWorkspace.sharedWorkspace().frontmostApplication().localizedName()


def _window_count() -> int:
    import Quartz
    opt = (Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements)
    return len(Quartz.CGWindowListCopyWindowInfo(opt, Quartz.kCGNullWindowID) or [])


def _osa_roundtrip() -> None:
    subprocess.run(["osascript", "-e", "return 1"], capture_output=True, timeout=5)


def _screencapture() -> None:
    tmp = os.path.join(tempfile.gettempdir(), "orrin-lat.png")
    subprocess.run(["screencapture", "-x", tmp], capture_output=True, timeout=10)


def run_suite(n: int = 20) -> dict:
    cases: list[tuple[str, Callable[[], object], int]] = [
        ("frontmost app (in-process)", _frontmost_app, n),
        ("window list (in-process)", _window_count, n),
        ("osascript roundtrip (subprocess)", _osa_roundtrip, n),
        ("screencapture (subprocess)", _screencapture, min(n, 5)),  # heavier; fewer samples
    ]
    return {name: measure(fn, iters) for name, fn, iters in cases}


def _main() -> None:
    res = run_suite()
    print(f"{'local action':<36}{'p50':>8}{'p95':>8}{'min':>8}{'max':>8}   (ms)")
    print("-" * 70)
    for name, m in res.items():
        print(f"{name:<36}{m['p50']:>8}{m['p95']:>8}{m['min']:>8}{m['max']:>8}")
    print("\nFor contrast, routing a request to Claude/Cursor is a full agent turn — typically several\n"
          "seconds. The local router answers the actions above without that round-trip.")


if __name__ == "__main__":
    _main()
