"""Machine-level access (capability) manager — probe, report, and help grant the macOS permissions
Orrin needs to do system/screen actions directly.

Reuses otto/preflight.py's probes (mic / accessibility / input monitoring / screen recording) and adds
an Automation/AppleEvents probe (the one preflight didn't cover — needed for osascript `tell
application`). Everything is defensive: a probe that can't run returns "unknown", never raises.

Run `python -m otto.capabilities` to print the current access report.
"""
from __future__ import annotations

import subprocess

from . import preflight

# capability -> (spoken label, preflight pane key, how to grant)
_CAPS = {
    "microphone": ("Microphone", "microphone",
                   "System Settings → Privacy & Security → Microphone"),
    "accessibility": ("Accessibility", "accessibility",
                      "System Settings → Privacy & Security → Accessibility"),
    "input_monitoring": ("Input Monitoring", "input_monitoring",
                         "System Settings → Privacy & Security → Input Monitoring"),
    "screen_recording": ("Screen Recording", "screen_recording",
                         "System Settings → Privacy & Security → Screen Recording"),
    "automation": ("Automation", "automation",
                   "System Settings → Privacy & Security → Automation"),
}


def automation_status() -> str:
    """granted / denied / unknown — can Orrin drive other apps via AppleEvents (osascript `tell`)?
    Behavioral probe: a trivial System Events query. May trigger the first-use TCC prompt."""
    try:
        p = subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to return name of first process'],
            capture_output=True, text=True, timeout=5)
    except Exception:
        return "unknown"
    if p.returncode == 0 and p.stdout.strip():
        return "granted"
    err = (p.stderr or "").lower()
    if "-1743" in err or "not allow" in err or "not authoriz" in err:
        return "denied"
    return "unknown"


def _status(cap: str) -> str:
    try:
        if cap == "microphone":
            s = preflight.mic_authorization_status()
            return "granted" if s == "authorized" else ("denied" if s == "denied" else "unknown")
        if cap == "accessibility":
            return "granted" if preflight.accessibility_trusted() else "denied"
        if cap == "input_monitoring":
            s = preflight.input_monitoring_status()
            return "granted" if s == "granted" else ("denied" if s == "denied" else "unknown")
        if cap == "screen_recording":
            s = preflight.screen_recording_status()
            return "granted" if s == "authorized" else ("denied" if s == "denied" else "unknown")
        if cap == "automation":
            return automation_status()
    except Exception:
        return "unknown"
    return "unknown"


def is_granted(cap: str) -> bool:
    return _status(cap) == "granted"


def capability_report() -> dict:
    out = {}
    for cap, (label, pane, how) in _CAPS.items():
        st = _status(cap)
        out[cap] = {"label": label, "status": st, "granted": st == "granted",
                    "pane": pane, "how_to_grant": how}
    return out


def summary(report: dict | None = None) -> str:
    rep = report or capability_report()
    have = [v["label"] for v in rep.values() if v["granted"]]
    missing = [v["label"] for v in rep.values() if not v["granted"]]
    parts = []
    if have:
        parts.append("I have " + ", ".join(have) + " access")
    if missing:
        lead = "but I'm missing " if have else "I'm missing "
        parts.append(lead + ", ".join(missing)
                     + " — open System Settings, Privacy & Security to grant them")
    return ((". ".join(parts) + ".") if parts else "I couldn't determine my access.")


def open_pane_for(cap: str) -> None:
    pane = _CAPS.get(cap, (None, None, None))[1]
    if pane:
        preflight.open_privacy_pane(pane)


def _main() -> None:
    import json
    rep = capability_report()
    print(json.dumps(rep, indent=2))
    print("\n" + summary(rep))


if __name__ == "__main__":
    _main()
