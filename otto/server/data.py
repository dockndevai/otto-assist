"""Live data for the dashboard cards — weather, GitHub activity, calendar, MCP count.

Everything here is best-effort and fast: network calls and shell-outs are bounded by short
timeouts and return ``None``/empty on any failure, so the dashboard simply hides a card when
its data isn't available (no empty boxes). No API keys required.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

_UA = {"User-Agent": "Orrin/0.2"}


def _get_json(url: str, timeout: float = 6.0) -> Any:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---- Weather (ip-api geolocation + open-meteo forecast + air quality) --------
_WMO = {
    0: ("Clear", "☀️"), 1: ("Mainly clear", "🌤"), 2: ("Partly cloudy", "⛅"), 3: ("Overcast", "☁️"),
    45: ("Fog", "🌫"), 48: ("Rime fog", "🌫"), 51: ("Light drizzle", "🌦"), 53: ("Drizzle", "🌦"),
    55: ("Heavy drizzle", "🌦"), 61: ("Light rain", "🌧"), 63: ("Rain", "🌧"), 65: ("Heavy rain", "🌧"),
    66: ("Freezing rain", "🌧"), 67: ("Freezing rain", "🌧"), 71: ("Light snow", "🌨"), 73: ("Snow", "🌨"),
    75: ("Heavy snow", "❄️"), 77: ("Snow grains", "🌨"), 80: ("Showers", "🌦"), 81: ("Showers", "🌦"),
    82: ("Heavy showers", "⛈"), 85: ("Snow showers", "🌨"), 86: ("Snow showers", "🌨"),
    95: ("Thunderstorm", "⛈"), 96: ("Thunderstorm", "⛈"), 99: ("Thunderstorm", "⛈"),
}


def _aqi_label(aqi: float) -> str:
    if aqi <= 50: return "Good"
    if aqi <= 100: return "Moderate"
    if aqi <= 150: return "Unhealthy (sensitive)"
    if aqi <= 200: return "Unhealthy"
    if aqi <= 300: return "Very unhealthy"
    return "Hazardous"


_weather_cache: dict[str, Any] = {"ts": 0, "data": None}


def weather() -> dict | None:
    """Current weather for the machine's approximate location. Cached ~10 min."""
    if _weather_cache["data"] and time.time() - _weather_cache["ts"] < 600:
        return _weather_cache["data"]
    try:
        geo = _get_json("http://ip-api.com/json/?fields=status,city,region,lat,lon", 5)
        if geo.get("status") != "success":
            return None
        lat, lon, city = geo["lat"], geo["lon"], geo.get("city", "")
        region = geo.get("region", "")
        fc = _get_json(
            f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
            "&current=temperature_2m,weather_code,wind_speed_10m,precipitation_probability"
            "&daily=temperature_2m_max,temperature_2m_min&timezone=auto", 6)
        cur, daily = fc.get("current", {}), fc.get("daily", {})
        code = int(cur.get("weather_code", 0))
        cond, icon = _WMO.get(code, ("—", "🌡"))
        aqi = None
        try:
            aq = _get_json(
                f"https://air-quality-api.open-meteo.com/v1/air-quality?latitude={lat}"
                f"&longitude={lon}&current=us_aqi", 5)
            aqi = aq.get("current", {}).get("us_aqi")
        except Exception:
            pass
        data = {
            "city": f"{city}, {region}" if region else city,
            "temp": round(cur.get("temperature_2m", 0)),
            "condition": cond, "icon": icon,
            "hi": round((daily.get("temperature_2m_max") or [0])[0]),
            "lo": round((daily.get("temperature_2m_min") or [0])[0]),
            "precip": cur.get("precipitation_probability", 0),
            "wind": round(cur.get("wind_speed_10m", 0)),
            "aqi": round(aqi) if aqi is not None else None,
            "aqi_label": _aqi_label(aqi) if aqi is not None else "",
        }
        _weather_cache.update(ts=time.time(), data=data)
        return data
    except Exception:
        return None


# ---- GitHub activity (gh CLI, for the focused project) -----------------------
def _run(args: list[str], cwd: str, timeout: float = 6.0) -> str | None:
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


def github_activity(cwd: str) -> dict | None:
    """Open PRs, issues assigned to you, recent commits for the repo at ``cwd``. None if not a
    GitHub repo / gh unavailable."""
    if not cwd or not Path(cwd).is_dir():
        return None
    view = _run(["gh", "repo", "view", "--json", "nameWithOwner,url"], cwd, 6)
    if not view:
        return None
    try:
        repo = json.loads(view)
    except Exception:
        return None

    def _count(out: str | None) -> int:
        try:
            return len(json.loads(out)) if out else 0
        except Exception:
            return 0

    prs = _count(_run(["gh", "pr", "list", "--state", "open", "--json", "number", "--limit", "100"], cwd))
    issues = _count(_run(["gh", "issue", "list", "--state", "open", "--assignee", "@me",
                          "--json", "number", "--limit", "100"], cwd))
    commits_out = _run(["git", "log", "--since=7 days ago", "--oneline"], cwd, 5)
    commits = len([l for l in (commits_out or "").splitlines() if l.strip()])
    return {"repo": repo.get("nameWithOwner", ""), "url": repo.get("url", ""),
            "open_prs": prs, "issues_assigned": issues, "recent_commits": commits}


# ---- Calendar (EventKit, macOS) ----------------------------------------------
def calendar_events(limit: int = 3) -> list[dict] | None:
    """Upcoming events for the rest of today. None if EventKit is unavailable or access isn't
    granted (the dashboard then hides the card). Access is requested on the main thread."""
    try:
        import EventKit  # type: ignore
        from Foundation import NSDate  # type: ignore
    except Exception:
        return None
    try:
        store = EventKit.EKEventStore.alloc().init()
        status = EventKit.EKEventStore.authorizationStatusForEntityType_(0)  # 0 = .event
        if int(status) != 3:      # 3 = authorized / fullAccess
            return None
        now = NSDate.date()
        end = NSDate.dateWithTimeIntervalSinceNow_(60 * 60 * 24)  # next 24h
        cals = store.calendarsForEntityType_(0)
        pred = store.predicateForEventsWithStartDate_endDate_calendars_(now, end, cals)
        evs = store.eventsMatchingPredicate_(pred) or []
        out = []
        for e in sorted(evs, key=lambda x: x.startDate().timeIntervalSince1970()):
            if e.isAllDay():
                continue
            title = str(e.title() or "Event")
            start = e.startDate().timeIntervalSince1970()
            loc = str(e.location() or "")
            col = None
            try:
                c = e.calendar().color()
                if c is not None:
                    col = "#%02x%02x%02x" % (int(c.redComponent() * 255),
                                             int(c.greenComponent() * 255), int(c.blueComponent() * 255))
            except Exception:
                pass
            out.append({"title": title, "start": start, "location": loc, "color": col})
            if len(out) >= limit:
                break
        return out
    except Exception:
        return None


def request_calendar_access() -> None:
    """Fire the Calendar permission prompt (call from the main thread)."""
    try:
        import EventKit  # type: ignore
        store = EventKit.EKEventStore.alloc().init()
        if hasattr(store, "requestFullAccessToEventsWithCompletion_"):
            store.requestFullAccessToEventsWithCompletion_(lambda granted, err: None)
        else:
            store.requestAccessToEntityType_completion_(0, lambda granted, err: None)
    except Exception:
        pass


# ---- MCP connector count (from the user's Claude config) ---------------------
def mcp_count(cwd: str | None = None) -> int:
    """Best-effort count of MCP servers the user's Claude has configured (user + project)."""
    names: set[str] = set()
    for p in (Path.home() / ".claude.json", Path.home() / ".claude" / "settings.json"):
        try:
            d = json.loads(p.read_text())
            names.update((d.get("mcpServers") or {}).keys())
        except Exception:
            pass
    if cwd:
        try:
            d = json.loads((Path(cwd) / ".mcp.json").read_text())
            names.update((d.get("mcpServers") or {}).keys())
        except Exception:
            pass
    return len(names)
