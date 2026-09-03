"""A tiny asyncio pub/sub bus.

The orchestrator and the agent publish events; every connected browser (and any other
subscriber) receives them. Events are plain dicts with a "type" field:

    status              {"state": "idle|listening|thinking|speaking", "text": str}
    transcript_partial  {"role": "user", "text": str}
    transcript_final    {"role": "user|assistant", "text": str}
    assistant_delta     {"text": str}                 # streamed token chunk
    tool_use            {"name": str, "detail": str}
    canvas_render       {"format": "html|markdown", "content": str}
    ask_user            {"question": str}             # awaiting a spoken/typed answer
    notify              {"title": str, "body": str}
    project             {"path": str, "name": str}    # focused project (cowork space)
    session             {"session_id": str|None, "title": str}  # focused chat
    workspaces          {"items": [{project,name,state,focused,session_id}], "focused": str,
                         "max": int}                  # the parallel-workspace pool
    log                 {"level": str, "logger": str, "msg": str, "ts": float}  # dashboard Logs drawer
    preflight           {"checks": [{key,label,status,detail,required,fix,pane}],
                         "ready": bool, "done": bool, "first_run": bool}  # setup/health panel
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._sinks: list[Callable[[dict[str, Any]], None]] = []

    def add_sink(self, fn: Callable[[dict[str, Any]], None]) -> None:
        """Register a synchronous side-effect (e.g. history persistence) run on publish."""
        self._sinks.append(fn)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(q)

    def publish(self, event: dict[str, Any]) -> None:
        """Fan an event out to all subscribers (drops on a full/slow queue)."""
        for fn in self._sinks:
            try:
                fn(event)
            except Exception:
                pass
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    # Convenience helpers ---------------------------------------------------
    # `ws` tags an event with the workspace (project path) it came from, so the dashboard
    # can keep a separate chat window per parallel workspace. None = the single/global view.
    def status(self, state: str, text: str = "") -> None:
        self.publish({"type": "status", "state": state, "text": text})

    def transcript(self, role: str, text: str, final: bool = True,
                   ws: str | None = None) -> None:
        kind = "transcript_final" if final else "transcript_partial"
        self.publish({"type": kind, "role": role, "text": text, "ws": ws})

    def assistant_delta(self, text: str, ws: str | None = None) -> None:
        self.publish({"type": "assistant_delta", "text": text, "ws": ws})

    def tool_use(self, name: str, detail: str = "", ws: str | None = None) -> None:
        self.publish({"type": "tool_use", "name": name, "detail": detail, "ws": ws})

    def canvas(self, content: str, fmt: str = "html", ws: str | None = None) -> None:
        self.publish({"type": "canvas_render", "format": fmt, "content": content, "ws": ws})

    def notify(self, title: str, body: str = "") -> None:
        self.publish({"type": "notify", "title": title, "body": body})
