"""Parallel workspaces — each is a project running its own Claude, concurrently.

A workspace bundles a project directory + its own ``ClaudeBrain`` (its own CLI subprocess,
so several workspaces run truly in parallel) + a per-workspace lock (same-workspace turns
serialize; different workspaces don't). Only the *focused* workspace streams to the screen
and speaks; background workspaces run headless and notify when done.

``WorkspaceContext`` is the ``AssistantContext`` handed to each brain's voice/screen tools,
so a tool call (say / render_canvas / …) is tagged with the workspace it came from and the
orchestrator can route it (stream only if focused, else store/suppress).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from .brain.agent import ClaudeBrain
from .config import Settings


class WorkspaceContext:
    """AssistantContext bound to one workspace; forwards tool callbacks to the orchestrator
    tagged with the workspace path."""

    def __init__(self, orch: Any, project: str) -> None:
        self.orch = orch
        self.project = project

    async def say(self, text: str) -> None:
        await self.orch._ws_say(self.project, text)

    async def ask_user(self, question: str) -> str:
        return await self.orch._ws_ask_user(self.project, question)

    async def confirm(self, action: str) -> bool:
        return await self.orch._ws_confirm(self.project, action)

    def render_canvas(self, content: str, fmt: str = "html") -> None:
        self.orch._ws_render(self.project, content, fmt)

    def set_status(self, text: str) -> None:
        self.orch._ws_set_status(self.project, text)

    def set_mood(self, level: str, note: str = "") -> None:
        self.orch.set_mood(level, note)

    def notify(self, title: str, body: str = "") -> None:
        self.orch.notify(title, body)

    async def remember_fact(self, fact: str, scope: str = "project") -> bool:
        return await self.orch._ws_remember_fact(self.project, fact, scope)

    async def remember_document(self, title: str, content: str) -> str:
        return await self.orch._ws_remember_document(self.project, title, content)

    async def suggest(self, text: str, kind: str = "action", severity: str = "low",
                      rationale: str = "") -> str:
        return await self.orch._ws_suggest(self.project, text, kind, severity, rationale)

    async def resolve_suggestion(self, suggestion_id: str, status: str = "done") -> str:
        return await self.orch._ws_resolve_suggestion(suggestion_id, status)

    async def remind(self, message: str, delay_seconds: float,
                     repeat_seconds: float = 0.0) -> str:
        return await self.orch._ws_remind(message, delay_seconds, repeat_seconds)

    async def list_reminders(self) -> str:
        return await self.orch._ws_list_reminders()

    async def cancel_reminder(self, reminder_id: str) -> str:
        return await self.orch._ws_cancel_reminder(reminder_id)


class Workspace:
    """One project running its own Claude chat, in parallel with others."""

    def __init__(self, orch: Any, settings: Settings, project: str, *,
                 resume: str | None = None, new_chat: bool = False,
                 read_only: bool = False) -> None:
        self.project = project
        self.name = Path(project).name or project
        self.read_only = read_only
        self.ctx = WorkspaceContext(orch, project)
        self.brain = ClaudeBrain(self.ctx, settings, resume=resume,
                                 new_chat=new_chat, cwd=project, read_only=read_only)
        self.state = "idle"                 # idle | thinking | speaking | working
        self.lock = asyncio.Lock()          # serialize this workspace's own turns
        self.last_canvas: str | None = None  # last visual it rendered (shown when focused)
        self.last_reply = ""                # last spoken/summary line (for the done-notify)
        self._last_preamble: str | None = None  # memory block last injected into this chat

    @property
    def session_id(self) -> str | None:
        return getattr(self.brain, "session_id", None)

    async def rebind(self, settings: Settings, *, resume: str | None = None,
                     new_chat: bool = False) -> None:
        """Point this workspace at a different chat (new brain, same project cwd)."""
        try:
            await self.brain.disconnect()
        except Exception:
            pass
        self.brain = ClaudeBrain(self.ctx, settings, resume=resume,
                                 new_chat=new_chat, cwd=self.project, read_only=self.read_only)
        self._last_preamble = None       # fresh chat → re-inject memory on its first turn
        await self.brain.connect()
