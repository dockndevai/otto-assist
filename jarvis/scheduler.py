"""Proactive scheduler — lets Jarvis speak to you unprompted.

Register reminders/check-ins that fire after a delay (or on an interval); each one speaks
via the orchestrator and pops a desktop notification. This is the third "talk back"
channel (alongside streamed replies and ask_user).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .orchestrator import Orchestrator


@dataclass
class Scheduler:
    orch: Orchestrator
    _tasks: set[asyncio.Task] = field(default_factory=set)

    def remind(self, delay_seconds: float, message: str, title: str = "Jarvis") -> None:
        """Speak `message` once, `delay_seconds` from now."""
        t = asyncio.get_running_loop().create_task(self._fire(delay_seconds, message, title))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def every(self, interval_seconds: float, message: str, title: str = "Jarvis") -> None:
        """Speak `message` repeatedly on an interval."""
        async def loop():
            while True:
                await asyncio.sleep(interval_seconds)
                await self._speak(message, title)
        t = asyncio.get_running_loop().create_task(loop())
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _fire(self, delay: float, message: str, title: str) -> None:
        await asyncio.sleep(delay)
        await self._speak(message, title)

    async def _speak(self, message: str, title: str) -> None:
        self.orch.notify(title, message)
        await self.orch.say(message)

    def shutdown(self) -> None:
        for t in self._tasks:
            t.cancel()
