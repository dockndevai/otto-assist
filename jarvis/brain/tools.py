"""Custom in-process tools — the bridge from the agent back to *you*.

These are built by a factory that closes over an `AssistantContext`, so each tool can
reach the running assistant (speak, ask, render to the screen). The context is any object
implementing the methods used below; in practice it's the Orchestrator.
"""

from __future__ import annotations

from typing import Any, Protocol

from claude_agent_sdk import create_sdk_mcp_server, tool

SERVER_NAME = "jarvis"


class AssistantContext(Protocol):
    async def say(self, text: str) -> None: ...
    async def ask_user(self, question: str) -> str: ...
    def render_canvas(self, content: str, fmt: str = "html") -> None: ...
    def set_status(self, text: str) -> None: ...
    def notify(self, title: str, body: str = "") -> None: ...


def _text(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}]}


def build_server(ctx: AssistantContext):
    """Create the SDK MCP server exposing Jarvis's self-directed tools."""

    @tool(
        "say",
        "Speak a short proactive update to the user out loud without ending your turn. "
        "Use for progress notes like 'still working on that, one moment'.",
        {"text": str},
    )
    async def say(args: dict[str, Any]) -> dict[str, Any]:
        await ctx.say(args["text"])
        return _text("spoken")

    @tool(
        "ask_user",
        "Ask the user a question and wait for their answer. Use this whenever you need "
        "clarification, a decision, or confirmation before a consequential action. "
        "Returns the user's answer as text.",
        {"question": str},
    )
    async def ask_user(args: dict[str, Any]) -> dict[str, Any]:
        answer = await ctx.ask_user(args["question"])
        return _text(answer or "(no answer)")

    @tool(
        "render_canvas",
        "Show visual content to the user on the dashboard canvas. Pass self-contained "
        "HTML (charts, tables, formatted results) or Markdown. This is how you 'show' "
        "the user anything visual.",
        {"content": str, "format": str},
    )
    async def render_canvas(args: dict[str, Any]) -> dict[str, Any]:
        fmt = args.get("format") or "html"
        ctx.render_canvas(args["content"], fmt)
        return _text(f"rendered {fmt} to canvas")

    @tool(
        "set_status",
        "Set the short status line shown on the dashboard (e.g. 'researching flights').",
        {"text": str},
    )
    async def set_status(args: dict[str, Any]) -> dict[str, Any]:
        ctx.set_status(args["text"])
        return _text("status updated")

    @tool(
        "notify",
        "Send a desktop notification (macOS) — useful for finished background work.",
        {"title": str, "body": str},
    )
    async def notify(args: dict[str, Any]) -> dict[str, Any]:
        ctx.notify(args["title"], args.get("body", ""))
        return _text("notified")

    return create_sdk_mcp_server(
        name=SERVER_NAME,
        version="0.1.0",
        tools=[say, ask_user, render_canvas, set_status, notify],
    )


# Fully-qualified tool names, for allowed_tools / permission logic.
def qualified_names() -> list[str]:
    names = ["say", "ask_user", "render_canvas", "set_status", "notify"]
    return [f"mcp__{SERVER_NAME}__{n}" for n in names]
