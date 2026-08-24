"""The Brain seam — Otto is wiring; the brain is whatever agent runs the reasoning.

Only ClaudeBrain exists today, but the orchestrator depends on this protocol so other
local agents can be dropped in later without touching the wiring.

`send()` yields small events the orchestrator forwards to voice + screen:
    {"type": "delta",    "text": str}                 # streamed spoken text
    {"type": "tool_use", "name": str, "detail": str}  # a tool/subagent was invoked
    {"type": "result",   "is_error": bool, ...}       # end of turn
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Protocol, runtime_checkable


@runtime_checkable
class Brain(Protocol):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def interrupt(self) -> None: ...
    def send(self, text: str) -> AsyncIterator[dict[str, Any]]: ...
