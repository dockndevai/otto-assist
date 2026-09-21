"""Proactive scheduler — reminders, timers, and recurring nudges.

Lets Orrin speak to you unprompted at a future time: "remind me in 20 minutes", "set a
timer for 10 minutes", "every 30 minutes tell me to stand up". This is the third "talk
back" channel (alongside streamed replies and ask_user), and the one thing that makes it
feel like an assistant instead of a request/response bot.

Reminders are PERSISTED to ``OTTO_HOME/reminders.json`` and reloaded on start, so a
"remind me in 2 hours" survives a restart, crash, or reboot. On reload, a one-shot that
came due while Orrin was down fires shortly after start (marked as missed); a recurring
one rolls forward to its next future occurrence.

Each reminder fires by popping a desktop notification AND speaking the message.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from pathlib import Path
from typing import Any


class Scheduler:
    def __init__(self, orch: Any, store_path: Path) -> None:
        self.orch = orch
        self._path = Path(store_path)
        self._reminders: dict[str, dict] = {}      # id -> record
        self._tasks: dict[str, asyncio.Task] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- lifecycle --------------------------------------------------------
    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Load persisted reminders and arm them. Call once from the orchestrator's loop."""
        self._loop = loop
        self._reminders = self._load()
        now = time.time()
        changed = False
        for rec in list(self._reminders.values()):
            if not rec.get("repeat") and float(rec.get("fire_at", 0)) < now:
                # Missed a one-shot while we were down — fire it a couple seconds after boot.
                rec["missed"] = True
                rec["fire_at"] = now + 2.0
                changed = True
            elif rec.get("repeat"):
                rec["fire_at"] = self._roll_forward(float(rec["fire_at"]), float(rec["repeat"]), now)
                changed = True
            self._arm(rec)
        if changed:
            self._save()

    def shutdown(self) -> None:
        for t in self._tasks.values():
            t.cancel()
        self._tasks.clear()

    # -- public API (called via the orchestrator from the `remind` tool) --
    def add(self, delay_seconds: float, message: str, *, title: str = "Orrin",
            repeat_seconds: float = 0.0) -> dict:
        """Schedule a reminder `delay_seconds` from now (repeating every `repeat_seconds` if > 0).
        Returns the saved record."""
        delay = max(1.0, float(delay_seconds))
        repeat = max(0.0, float(repeat_seconds or 0.0))
        rid = secrets.token_hex(3)
        rec = {"id": rid, "fire_at": time.time() + delay, "message": (message or "").strip(),
               "title": (title or "Orrin").strip() or "Orrin", "repeat": repeat,
               "created": time.time()}
        self._reminders[rid] = rec
        self._save()
        self._arm(rec)
        return rec

    def cancel(self, rid: str) -> dict | None:
        """Cancel a reminder by id (or a unique id prefix). Returns the removed record or None."""
        rid = (rid or "").strip().lower()
        match = None
        if rid in self._reminders:
            match = rid
        else:
            hits = [k for k in self._reminders if k.startswith(rid)] if rid else []
            if len(hits) == 1:
                match = hits[0]
        if not match:
            return None
        rec = self._reminders.pop(match, None)
        t = self._tasks.pop(match, None)
        if t:
            t.cancel()
        self._save()
        return rec

    def active(self) -> list[dict]:
        """Pending reminders, soonest first."""
        return sorted(self._reminders.values(), key=lambda r: r.get("fire_at", 0))

    # -- internals --------------------------------------------------------
    def _arm(self, rec: dict) -> None:
        if self._loop is None:
            return
        rid = rec["id"]
        old = self._tasks.pop(rid, None)
        if old:
            old.cancel()
        self._tasks[rid] = self._loop.create_task(self._run(rid))

    async def _run(self, rid: str) -> None:
        try:
            while True:
                rec = self._reminders.get(rid)
                if rec is None:
                    return
                delay = max(0.0, float(rec["fire_at"]) - time.time())
                await asyncio.sleep(delay)
                rec = self._reminders.get(rid)
                if rec is None:
                    return
                await self._fire(rec)
                if float(rec.get("repeat") or 0) > 0:
                    rec["fire_at"] = time.time() + float(rec["repeat"])
                    rec.pop("missed", None)
                    self._save()
                    continue                       # loop for the next occurrence
                self._reminders.pop(rid, None)     # one-shot done
                self._tasks.pop(rid, None)
                self._save()
                return
        except asyncio.CancelledError:
            return
        except Exception as e:
            import sys
            print(f"[otto] reminder {rid} failed: {e!r}", file=sys.stderr, flush=True)

    async def _fire(self, rec: dict) -> None:
        msg = rec.get("message") or "Reminder"
        title = rec.get("title") or "Orrin"
        spoken = (f"Missed reminder: {msg}" if rec.get("missed") else msg)
        try:
            self.orch.notify(title, msg)
        except Exception:
            pass
        try:
            await self.orch.say(spoken)
        except Exception:
            pass

    @staticmethod
    def _roll_forward(fire_at: float, repeat: float, now: float) -> float:
        if repeat <= 0 or fire_at >= now:
            return fire_at
        missed = (now - fire_at) // repeat + 1
        return fire_at + missed * repeat

    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self._path.read_text())
            if isinstance(data, list):
                return {r["id"]: r for r in data if isinstance(r, dict) and r.get("id")}
        except Exception:
            pass
        return {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(list(self._reminders.values()), indent=2))
            tmp.replace(self._path)
        except Exception:
            pass
