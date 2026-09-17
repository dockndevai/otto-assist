"""Audio output-route detection (macOS).

Full-duplex voice — keeping the mic live while Orrin speaks so you can say "stop" mid-sentence —
is only safe when the mic can't hear the output. On the **built-in speakers** the mic picks up
Orrin's own voice and loops; on **headphones / AirPods / an external device** there's no acoustic
path back into the mic, so barge-in is safe.

`output_is_external()` tells the orchestrator which case it's in, so barge-in can auto-enable only
when safe (see issue #56). Best-effort and cached: on any error it returns False (assume built-in
speakers → the conservative half-duplex path), so a detection hiccup never enables an echo loop.
"""

from __future__ import annotations

import json
import re
import subprocess
import time

# Names that mean the built-in speakers (an echo path back into the built-in mic).
_BUILTIN_RE = re.compile(r"(macbook.*speakers|built[- ]?in|internal speakers|^speakers$)", re.I)

_cache: tuple[float, bool] = (0.0, False)   # (timestamp, is_external)
_TTL = 4.0                                   # re-check at most every few seconds


def _default_output_name() -> str | None:
    """The current default audio OUTPUT device name, or None if it can't be determined."""
    try:
        out = subprocess.run(
            ["system_profiler", "SPAudioDataType", "-json"],
            capture_output=True, text=True, timeout=4.0)
        if out.returncode != 0 or not out.stdout:
            return None
        data = json.loads(out.stdout)
        for top in data.get("SPAudioDataType", []):
            for dev in top.get("_items", []):
                if dev.get("coreaudio_default_audio_output_device") == "spaudio_yes":
                    return dev.get("_name")
    except Exception:
        return None
    return None


def output_is_external(*, force: bool = False) -> bool:
    """True if the default output is NOT the built-in speakers (headphones/AirPods/external),
    i.e. barge-in is acoustically safe. Cached for a few seconds; `force` bypasses the cache."""
    global _cache
    now = time.monotonic()
    if not force and (now - _cache[0]) < _TTL:
        return _cache[1]
    name = _default_output_name()
    # Unknown → treat as built-in (conservative: don't enable barge-in on a guess).
    is_external = bool(name) and not _BUILTIN_RE.search(name or "")
    _cache = (now, is_external)
    return is_external


def output_route_name() -> str:
    """Human label for the current output route (for the dashboard / logs)."""
    name = _default_output_name()
    if not name:
        return "unknown"
    return name
