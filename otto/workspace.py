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

from .brain.factory import make_brain
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
                 read_only: bool = False, write_mode: bool = False,
                 engine: str | None = None) -> None:
        self.project = project
        self.name = Path(project).name or project
        # Dashboard/window id for this workspace's transcript events. Defaults to the project path
        # (one window per project). A parallel same-project workspace overrides this so its events
        # land in their own lane instead of interleaving with the focused turn's chat.
        self.tag = project
        self.read_only = read_only
        # Autonomous write mode (the background teammate / steward, #71/#72): edits + safe git/test
        # + PR-create on a feature branch, gated by the autonomy allowlist in the brain.
        self.write_mode = write_mode
        self.ctx = WorkspaceContext(orch, project)
        # Engine-agnostic: the factory picks Claude (SDK) or any configured CLI agent for this cwd.
        self.engine, self.brain = make_brain(
            self.ctx, settings, cwd=project, resume=resume, new_chat=new_chat,
            read_only=read_only, write_mode=write_mode, engine=engine)
        self.state = "idle"                 # idle | thinking | speaking | working
        self.lock = asyncio.Lock()          # serialize this workspace's own turns
        self.last_canvas: str | None = None  # last visual it rendered (shown when focused)
        self.last_reply = ""                # last spoken/summary line (for the done-notify)
        self.last_command = ""              # last focused user command (topic hint for auto-fresh)
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
        self.engine, self.brain = make_brain(
            self.ctx, settings, cwd=self.project, resume=resume, new_chat=new_chat,
            read_only=self.read_only, write_mode=self.write_mode, engine=self.engine)
        self._last_preamble = None       # fresh chat → re-inject memory on its first turn
        await self.brain.connect()
