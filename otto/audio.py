"""Audio output-route classification (macOS).

Full-duplex voice — keeping the mic live while Orrin speaks so you can say "stop" mid-sentence —
is only acoustically safe when the mic can't hear the output. On **built-in speakers, a Studio
Display, an external/Bluetooth speaker, or display (HDMI/DisplayPort) audio** the mic picks up
Orrin's own voice and loops; only on a **personal listening device (wired/USB headphones, AirPods
or a Bluetooth headset)** is there no acoustic path back into the mic.

`classify_output_route()` returns one of ISOLATED / ECHOING / UNKNOWN and is the input the
orchestrator's AudioMode resolver reads. It is an **allowlist**: a route is ISOLATED only when it
looks like a personal listening device; everything else (including anything unrecognized) is treated
as ECHOING, so a detection miss fails closed to the safe (half-duplex) path.

`system_profiler` is slow, so classification is **cached** and refreshed only off the hot path:
`refresh_route()` (which runs the subprocess) is called at startup and from a background task;
`classify_output_route()` reads the cached value only and never blocks a speech/TTS decision.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time

# Route classes.
ISOLATED = "isolated"   # headphones / AirPods / headset — full-duplex safe without AEC
ECHOING = "echoing"     # speakers / display / anything with an open-air acoustic path
UNKNOWN = "unknown"     # couldn't determine → callers treat as ECHOING (fail closed)

# A personal listening device: no open-air speaker, so no echo path into the mic.
_HEADSET_RE = re.compile(
    r"(head ?phone|air ?pod|ear ?phone|ear ?bud|\bbuds\b|head ?set|\bwh-\d|\bwf-\d|\bqc\s?\d)", re.I)
# Transport kinds that are an open-air speaker / display path (echo risk).
_ECHO_TRANSPORT_RE = re.compile(r"(builtin|hdmi|displayport|airplay|thunderbolt)", re.I)

_lock = threading.Lock()
# (timestamp, route_class, device_name)
_cache: tuple[float, str, str] = (0.0, UNKNOWN, "")


def _default_output() -> tuple[str | None, str]:
    """(name, transport) of the current default audio OUTPUT device; ('', '') if indeterminate."""
    try:
        out = subprocess.run(
            ["system_profiler", "SPAudioDataType", "-json"],
            capture_output=True, text=True, timeout=4.0)
        if out.returncode != 0 or not out.stdout:
            return None, ""
        data = json.loads(out.stdout)
        for top in data.get("SPAudioDataType", []):
            for dev in top.get("_items", []):
                if dev.get("coreaudio_default_audio_output_device") == "spaudio_yes":
                    return dev.get("_name"), str(dev.get("coreaudio_device_transport", ""))
    except Exception:
        return None, ""
    return None, ""


def _classify(name: str | None, transport: str) -> str:
    """Map a device (name + transport) to a RouteClass. Allowlist: ISOLATED only for a clear
    personal listening device; a named open-air device is ECHOING; no name is UNKNOWN."""
    if not name:
        return UNKNOWN
    if _HEADSET_RE.search(name):
        return ISOLATED
    if _ECHO_TRANSPORT_RE.search(transport):
        return ECHOING
    # A Bluetooth/USB device whose name isn't a headset (e.g. a Bluetooth speaker), a "Studio
    # Display", bare "Speakers", etc. — treat as echoing (fail closed).
    return ECHOING


def refresh_route(*, force: bool = False, ttl: float = 4.0) -> str:
    """Re-read the default output and update the cache (BLOCKS on system_profiler). Call this off
    the hot path only — at startup and from a background refresh/route-change task. Returns the
    freshly cached RouteClass."""
    global _cache
    now = time.monotonic()
    if not force and (now - _cache[0]) < ttl:
        return _cache[1]
    name, transport = _default_output()
    route = _classify(name, transport)
    with _lock:
        _cache = (now, route, name or "")
    return route


def classify_output_route() -> str:
    """The most recently cached RouteClass. NON-BLOCKING — never runs system_profiler; returns
    UNKNOWN until the first refresh_route(). The AudioMode resolver reads only this."""
    with _lock:
        return _cache[1]


def output_route_name() -> str:
    """Human label for the current output route (cached; for the dashboard / logs)."""
    with _lock:
        return _cache[2] or "unknown"


def output_is_external() -> bool:
    """Backwards-compatible shim: True only for an acoustically isolated route (cached)."""
    return classify_output_route() == ISOLATED
