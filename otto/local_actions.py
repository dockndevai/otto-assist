"""Local intent router — do simple Mac actions DIRECTLY on the machine, with no Claude/Cursor
round-trip, for speed.

`LocalActions.match(text)` does deterministic regex/keyword matching and returns an `ActionMatch`
(a bound async executor + metadata) or `None`. `None` means "not a recognized local action" and the
orchestrator falls through to the engine unchanged — so this never hijacks a real coding/research
request. Executors shell out via argv only (`create_subprocess_exec`, never a shell string), so there
is no command-injection surface. Consequential actions are flagged; the caller gates them on a spoken
confirm.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from .orchestrator import Orchestrator

# An executor does the action and returns a short line to speak/show back ("" = nothing to say).
Executor = Callable[[], Awaitable[str]]


@dataclass
class ActionMatch:
    executor: Executor
    description: str          # human description, e.g. "open Safari"
    category: str             # one of: apps / timers / media / system / info
    consequential: bool = False


# ---- duration + clock parsing ------------------------------------------------
_UNIT_SECONDS = {
    "sec": 1, "secs": 1, "second": 1, "seconds": 1, "s": 1,
    "min": 60, "mins": 60, "minute": 60, "minutes": 60, "m": 60,
    "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600, "h": 3600,
}
_NUM_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30,
    "forty": 40, "forty-five": 45, "forty five": 45, "sixty": 60, "ninety": 90,
}
_DUR_RE = re.compile(
    r"(?P<num>\d+(?:\.\d+)?|a|an|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|"
    r"twenty|thirty|forty(?:[\s-]five)?|forty|sixty|ninety)\s*"
    r"(?P<unit>seconds?|secs?|minutes?|mins?|hours?|hrs?|[smh])\b", re.I)


def parse_duration(text: str) -> float | None:
    """Sum every '<n> <unit>' phrase in `text` → seconds (e.g. '1 hour 30 minutes'). Also handles
    'half an hour'. Returns None if no duration is present."""
    t = text.lower()
    total = 0.0
    found = False
    half_matches = len(re.findall(r"\bhalf\s+an?\s+hour\b", t))
    if half_matches:
        total += 1800.0 * half_matches
        found = True
        t = re.sub(r"\bhalf\s+an?\s+hour\b", " ", t)   # don't let "an hour" re-match below
    for mo in _DUR_RE.finditer(t):
        num_raw, unit = mo.group("num"), mo.group("unit").lower()
        num = _NUM_WORDS.get(num_raw.lower()) if not re.match(r"[\d.]", num_raw) else float(num_raw)
        if num is None:
            continue
        total += float(num) * _UNIT_SECONDS.get(unit, _UNIT_SECONDS.get(unit.rstrip("s"), 0))
        found = True
    return total if (found and total >= 1) else None


_CLOCK_RE = re.compile(r"\b(?:at\s+)?(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm|a\.m\.|p\.m\.)\b", re.I)


def parse_clock_delay(text: str, now: datetime | None = None) -> float | None:
    """'at 7am' / '7:30 pm' → seconds until the next occurrence of that clock time. None if absent."""
    mo = _CLOCK_RE.search(text)
    if not mo:
        return None
    now = now or datetime.now()
    hour = int(mo.group("h")) % 12
    if mo.group("ap").lower().startswith("p"):
        hour += 12
    minute = int(mo.group("m") or 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


class LocalActions:
    """Deterministic router from an utterance to a direct Mac action (no engine)."""

    # Matchers run in order; the first whose guard returns an ActionMatch wins.
    _RE_OPEN = re.compile(r"^(?:open|launch|start|fire up|switch to|go to|bring up)\s+(?P<app>.+)$", re.I)
    _RE_QUIT = re.compile(r"^(?:quit|exit|close)\s+(?P<app>.+?)(?:\s+app)?$", re.I)
    _RE_FORCE_QUIT = re.compile(r"^(?:force\s*quit|kill)\s+(?P<app>.+?)(?:\s+app)?$", re.I)
    _RE_TIMER = re.compile(r"\b(?:set\s+(?:a\s+)?timer|timer)\b", re.I)
    _RE_ALARM = re.compile(r"\b(?:set\s+(?:an?\s+)?alarm|wake me|alarm)\b", re.I)
    _RE_REMIND = re.compile(r"\bremind me\b", re.I)
    _RE_LIST_TIMERS = re.compile(r"\b(?:what|which|list|show|any)\b.*\b(?:timers?|alarms?|reminders?)\b", re.I)
    _RE_CANCEL_TIMER = re.compile(r"\bcancel\b.*\b(?:timer|alarm|reminder)\b", re.I)
    _RE_TIME = re.compile(r"\b(?:what(?:'?s| is)?|tell me|current)\b.*\btime\b", re.I)
    _RE_DATE = re.compile(r"\b(?:what(?:'?s| is)?|tell me|current|today'?s?)\b.*\b(?:date|day)\b", re.I)
    _RE_SCREENSHOT = re.compile(r"\b(?:take|grab|capture)\b.*\bscreenshot\b|\bscreenshot\b", re.I)
    _RE_LOCK = re.compile(r"\block\b.*\b(?:screen|mac|computer|it)\b|\block the screen\b", re.I)
    _RE_SLEEP_DISPLAY = re.compile(r"\bsleep\b.*\b(?:display|screen)\b|\bturn off the (?:display|screen)\b", re.I)
    _RE_SLEEP_MAC = re.compile(r"\b(?:go to sleep|sleep the (?:mac|computer)|put .* to sleep)\b", re.I)
    _RE_EMPTY_TRASH = re.compile(r"\bempty\b.*\btrash\b", re.I)
    _RE_RESTART = re.compile(r"\brestart\b.*\b(?:mac|computer|machine)\b", re.I)
    _RE_SHUTDOWN = re.compile(r"\bshut\s*down\b.*\b(?:mac|computer|machine)\b|\bshut\s*down\b$", re.I)
    _RE_MUTE = re.compile(r"^(?:mute|silence)\b", re.I)
    _RE_UNMUTE = re.compile(r"^unmute\b", re.I)
    _RE_VOL_SET = re.compile(r"\b(?:set\s+)?volume\s+(?:to\s+)?(?P<n>\d{1,3})\s*%?\b", re.I)
    _RE_VOL_UP = re.compile(r"\b(?:volume up|turn (?:it|the volume) up|louder)\b", re.I)
    _RE_VOL_DOWN = re.compile(r"\b(?:volume down|turn (?:it|the volume) down|quieter|lower the volume)\b", re.I)
    _RE_PLAYPAUSE = re.compile(r"^(?:play|pause|resume|play music|pause music|stop music)\b", re.I)
    _RE_NEXT = re.compile(r"^(?:next|skip)(?:\s+(?:track|song))?\b", re.I)
    _RE_PREV = re.compile(r"^(?:previous|back)(?:\s+(?:track|song))?\b|^go back a (?:track|song)\b", re.I)

    _APP_DIRS = ["/Applications", "/System/Applications", "/Applications/Utilities",
                 "/System/Applications/Utilities"]
    # Common spoken → real app-name aliases.
    _APP_ALIASES = {
        "browser": "Safari", "the browser": "Safari", "chrome": "Google Chrome",
        "vs code": "Visual Studio Code", "vscode": "Visual Studio Code", "code": "Visual Studio Code",
        "terminal": "Terminal", "settings": "System Settings", "preferences": "System Settings",
        "system preferences": "System Settings", "music": "Music", "spotify": "Spotify",
        "notes": "Notes", "mail": "Mail", "calendar": "Calendar", "finder": "Finder",
        "messages": "Messages", "cursor": "Cursor", "slack": "Slack",
    }

    def __init__(self, orch: "Orchestrator") -> None:
        self.orch = orch
        self._apps_cache: tuple[float, dict[str, str]] = (0.0, {})

    # ---- public API ----------------------------------------------------------
    def match(self, text: str) -> ActionMatch | None:
        t = (text or "").strip()
        if not t:
            return None
        enabled = set(getattr(self.orch.settings.voice, "actions_enabled",
                              ["apps", "timers", "media", "system", "info"]))

        def on(cat: str) -> bool:
            return cat in enabled

        # --- info (time / date) ---
        if on("info"):
            if self._RE_TIME.search(t) and "timer" not in t.lower():
                return ActionMatch(self._exec_time, "tell the time", "info")
            if self._RE_DATE.search(t):
                return ActionMatch(self._exec_date, "tell the date", "info")

        # --- timers / reminders ---
        if on("timers"):
            if self._RE_LIST_TIMERS.search(t):
                return ActionMatch(self._exec_list_timers, "list timers", "timers")
            if self._RE_CANCEL_TIMER.search(t):
                return ActionMatch(self._exec_cancel_timer, "cancel a timer", "timers")
            if self._RE_REMIND.search(t):
                secs = parse_duration(t) or parse_clock_delay(t)
                if secs:
                    msg = self._remind_message(t)
                    return ActionMatch(lambda: self._exec_reminder(secs, msg),
                                       f"remind in {self.orch._fmt_when(secs)}", "timers")
            if self._RE_TIMER.search(t):
                secs = parse_duration(t)
                if secs:
                    return ActionMatch(lambda: self._exec_reminder(secs, "Timer", kind="Timer"),
                                       f"timer for {self.orch._fmt_when(secs)}", "timers")
            if self._RE_ALARM.search(t):
                secs = parse_clock_delay(t) or parse_duration(t)
                if secs:
                    return ActionMatch(lambda: self._exec_reminder(secs, "Alarm", kind="Alarm"),
                                       f"alarm in {self.orch._fmt_when(secs)}", "timers")

        # --- media / volume ---
        if on("media"):
            mo = self._RE_VOL_SET.search(t)
            if mo:
                n = max(0, min(100, int(mo.group("n"))))
                return ActionMatch(lambda: self._exec_volume_set(n), f"set volume to {n}%", "media")
            if self._RE_VOL_UP.search(t):
                return ActionMatch(lambda: self._exec_volume_step(+12), "turn the volume up", "media")
            if self._RE_VOL_DOWN.search(t):
                return ActionMatch(lambda: self._exec_volume_step(-12), "turn the volume down", "media")
            if self._RE_UNMUTE.search(t):
                return ActionMatch(lambda: self._exec_mute(False), "unmute", "media")
            if self._RE_MUTE.search(t):
                return ActionMatch(lambda: self._exec_mute(True), "mute", "media")
            if self._RE_NEXT.search(t):
                return ActionMatch(lambda: self._exec_media_key("next"), "next track", "media")
            if self._RE_PREV.search(t):
                return ActionMatch(lambda: self._exec_media_key("previous"), "previous track", "media")
            if self._RE_PLAYPAUSE.search(t):
                return ActionMatch(lambda: self._exec_media_key("playpause"), "play/pause", "media")

        # --- system ---
        if on("system"):
            if self._RE_EMPTY_TRASH.search(t):
                return ActionMatch(self._exec_empty_trash, "empty the Trash", "system", consequential=True)
            if self._RE_RESTART.search(t):
                return ActionMatch(self._exec_restart, "restart the Mac", "system", consequential=True)
            if self._RE_SHUTDOWN.search(t):
                return ActionMatch(self._exec_shutdown, "shut down the Mac", "system", consequential=True)
            if self._RE_SCREENSHOT.search(t):
                return ActionMatch(self._exec_screenshot, "take a screenshot", "system")
            if self._RE_LOCK.search(t) or self._RE_SLEEP_DISPLAY.search(t):
                return ActionMatch(self._exec_lock, "lock the screen", "system")
            if self._RE_SLEEP_MAC.search(t):
                return ActionMatch(self._exec_sleep_mac, "put the Mac to sleep", "system")

        # --- apps (resolve the name NOW so "open the pull request" falls through to the engine) ---
        if on("apps"):
            mo = self._RE_FORCE_QUIT.match(t)
            if mo:
                app = self._resolve_app(mo.group("app"))
                if app:
                    return ActionMatch(lambda: self._exec_force_quit(app), f"force quit {app}",
                                       "apps", consequential=True)
            mo = self._RE_QUIT.match(t)
            if mo:
                app = self._resolve_app(mo.group("app"))
                if app:
                    return ActionMatch(lambda: self._exec_quit(app), f"quit {app}", "apps")
            mo = self._RE_OPEN.match(t)
            if mo:
                app = self._resolve_app(mo.group("app"))
                if app:
                    return ActionMatch(lambda: self._exec_open(app), f"open {app}", "apps")
        return None

    # ---- app resolution ------------------------------------------------------
    def _installed_apps(self) -> dict[str, str]:
        now = time.monotonic()
        ts, cache = self._apps_cache
        if cache and now - ts < 60:
            return cache
        apps: dict[str, str] = {}
        dirs = list(self._APP_DIRS) + [os.path.expanduser("~/Applications")]
        for d in dirs:
            try:
                for name in os.listdir(d):
                    if name.endswith(".app"):
                        apps[name[:-4].lower()] = name[:-4]
            except OSError:
                continue
        self._apps_cache = (now, apps)
        return apps

    def _resolve_app(self, raw: str) -> str | None:
        name = re.sub(r"^(the|my|an?)\s+", "", (raw or "").strip().strip(".!?"), flags=re.I).strip()
        if not name:
            return None
        key = name.lower()
        if key in self._APP_ALIASES:
            return self._APP_ALIASES[key]
        apps = self._installed_apps()
        if key in apps:
            return apps[key]
        # prefix / contained match (e.g. "chrome" → "Google Chrome")
        for low, real in apps.items():
            if low == key or low.startswith(key) or key in low:
                return real
        return None

    # ---- executors -----------------------------------------------------------
    async def _run(self, *argv: str) -> int:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            return await proc.wait()
        except Exception:
            return -1

    async def _osa(self, *, script: list[str], args: list[str] | None = None) -> int:
        argv = ["osascript"]
        for ln in script:
            argv += ["-e", ln]
        if args:
            argv += ["--", *args]
        return await self._run(*argv)

    async def _exec_time(self) -> str:
        return "It's " + datetime.now().strftime("%-I:%M %p").lstrip("0")

    async def _exec_date(self) -> str:
        return datetime.now().strftime("It's %A, %B %-d.")

    async def _exec_reminder(self, secs: float, message: str, *, kind: str = "Reminder") -> str:
        if self.orch.scheduler is None:
            return "reminders aren't available right now"
        rec = self.orch.scheduler.add(float(secs), message, title=kind)
        self.orch.bus.publish({"type": "reminder_set", "item": rec})
        when = self.orch._fmt_when(float(secs))
        if kind == "Timer":
            return f"Timer set for {when}."
        if kind == "Alarm":
            return f"Alarm set for {when} from now."
        return f"Okay — I'll remind you in {when}."

    async def _exec_list_timers(self) -> str:
        return await self.orch._ws_list_reminders()

    async def _exec_cancel_timer(self) -> str:
        if self.orch.scheduler is None:
            return "reminders aren't available right now"
        items = self.orch.scheduler.active()
        if not items:
            return "You have no timers set."
        if len(items) == 1:
            rec = self.orch.scheduler.cancel(items[0]["id"])
            self.orch.bus.publish({"type": "reminder_cancelled", "id": items[0]["id"]})
            return f"Cancelled: {rec.get('message', rec['id']) if rec else items[0]['id']}."
        return f"You have {len(items)} timers — say which one by its id, or 'cancel all timers'."

    async def _exec_volume_set(self, n: int) -> str:
        await self._osa(script=["on run argv", "set volume output volume (item 1 of argv as integer)",
                                "end run"], args=[str(n)])
        return f"Volume {n}%."

    async def _exec_volume_step(self, delta: int) -> str:
        await self._osa(script=[
            "on run argv",
            "set v to output volume of (get volume settings)",
            "set nv to v + (item 1 of argv as integer)",
            "if nv > 100 then set nv to 100",
            "if nv < 0 then set nv to 0",
            "set volume output volume nv",
            "end run"], args=[str(delta)])
        return ""

    async def _exec_mute(self, muted: bool) -> str:
        await self._osa(script=["on run argv",
                                "set volume " + ("with" if muted else "without") + " output muted",
                                "end run"], args=["1"])
        return "Muted." if muted else "Unmuted."

    async def _exec_media_key(self, which: str) -> str:
        try:
            from pynput.keyboard import Controller, Key
            k = {"playpause": Key.media_play_pause, "next": Key.media_next,
                 "previous": Key.media_previous}[which]
            Controller().tap(k)
        except Exception:
            return ""
        return ""

    async def _exec_open(self, app: str) -> str:
        rc = await self._run("open", "-a", app)
        return f"Opened {app}." if rc == 0 else f"I couldn't open {app}."

    async def _exec_quit(self, app: str) -> str:
        await self._osa(script=["on run argv", "tell application (item 1 of argv) to quit", "end run"],
                        args=[app])
        return f"Quit {app}."

    async def _exec_force_quit(self, app: str) -> str:
        rc = await self._run("pkill", "-x", app)
        return f"Force quit {app}." if rc == 0 else f"Couldn't force quit {app}."

    async def _exec_screenshot(self) -> str:
        path = os.path.expanduser(datetime.now().strftime("~/Desktop/Screenshot %Y-%m-%d %H.%M.%S.png"))
        rc = await self._run("screencapture", "-x", path)
        if rc == 0:
            self.orch.notify("Screenshot saved", os.path.basename(path))
            return "Saved a screenshot to your Desktop."
        return "I couldn't take the screenshot."

    async def _exec_lock(self) -> str:
        await self._run("pmset", "displaysleepnow")
        return "Locking the screen."

    async def _exec_sleep_mac(self) -> str:
        await self._run("pmset", "sleepnow")
        return "Going to sleep."

    async def _exec_empty_trash(self) -> str:
        await self._osa(script=["tell application \"Finder\" to empty trash"])
        return "Trash emptied."

    async def _exec_restart(self) -> str:
        await self._osa(script=["tell application \"System Events\" to restart"])
        return "Restarting."

    async def _exec_shutdown(self) -> str:
        await self._osa(script=["tell application \"System Events\" to shut down"])
        return "Shutting down."

    # ---- helpers -------------------------------------------------------------
    def _remind_message(self, text: str) -> str:
        """Pull the task out of 'remind me [in X] to <task>' / 'remind me to <task> [in X]'."""
        mo = re.search(r"remind me\b.*?\bto\s+(?P<task>.+)$", text, re.I)
        task = mo.group("task").strip() if mo else ""
        # strip a trailing "in 10 minutes" the duration parser already consumed
        task = _DUR_RE.sub("", task)
        task = re.sub(r"\bin\s*$", "", task, flags=re.I).strip(" ,.")
        return task or "reminder"
