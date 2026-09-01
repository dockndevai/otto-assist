"""The Brain seam — Otto is wiring; the brain is whatever agent runs the reasoning.

Only ClaudeBrain exists today, but the orchestrator depends on this protocol so other
local agents can be dropped in later without touching the wiring.

`send()` yields small events the orchestrator forwards to voice + screen:
    {"type": "delta",    "text": str}                 # streamed spoken text
    {"type": "tool_use", "name": str, "detail": str}  # a tool/subagent was invoked
    {"type": "result",   "is_error": bool, "session_id": str | None, ...}  # end of turn

Brains that wrap Claude also expose a ``session_id`` attribute (the current chat's id, or
None until the first turn of a brand-new chat completes) so the orchestrator can track and
persist which chat is active.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Protocol, runtime_checkable


@runtime_checkable
class Brain(Protocol):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def interrupt(self) -> None: ...
    def send(self, text: str) -> AsyncIterator[dict[str, Any]]: ...
