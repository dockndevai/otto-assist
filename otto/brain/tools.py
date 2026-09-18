"""Custom in-process tools — the bridge from the agent back to *you*.

These are built by a factory that closes over an `AssistantContext`, so each tool can
reach the running assistant (speak, ask, render to the screen). The context is any object
implementing the methods used below; in practice it's the Orchestrator.
"""

from __future__ import annotations

from typing import Any, Protocol

from claude_agent_sdk import create_sdk_mcp_server, tool

# In-process voice/screen tools for the HOSTED brain. Named "voice" (not "otto") so it
# never collides with the externally-registered "otto" MCP a user's Claude may also load.
SERVER_NAME = "voice"


class AssistantContext(Protocol):
    async def say(self, text: str) -> None: ...
    async def ask_user(self, question: str) -> str: ...
    def render_canvas(self, content: str, fmt: str = "html") -> None: ...
    def set_status(self, text: str) -> None: ...
    def set_mood(self, level: str, note: str = "") -> None: ...
    def notify(self, title: str, body: str = "") -> None: ...
    async def remember_fact(self, fact: str, scope: str = "project") -> bool: ...
    async def remember_document(self, title: str, content: str) -> str: ...
    async def suggest(self, text: str, kind: str = "action", severity: str = "low",
                      rationale: str = "") -> str: ...
    async def resolve_suggestion(self, suggestion_id: str, status: str = "done") -> str: ...


def _text(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}]}


def build_server(ctx: AssistantContext):
    """Create the SDK MCP server exposing Otto's self-directed tools."""

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
        "set_mood",
        "Set the ambient color/mood of the dashboard to match the tone of what's happening. "
        "Use 'good' (green) for success or all-clear, 'info' (blue) for normal work, 'warn' "
        "(amber) for something needing attention, or 'critical' (red) for errors/serious "
        "issues. Optionally pass a short note. The dashboard also infers this automatically; "
        "call this to override when the topic's severity isn't obvious from activity.",
        {"level": str, "note": str},
    )
    async def set_mood(args: dict[str, Any]) -> dict[str, Any]:
        ctx.set_mood(args.get("level", "info"), args.get("note", ""))
        return _text("mood set")

    @tool(
        "notify",
        "Send a desktop notification (macOS) — useful for finished background work.",
        {"title": str, "body": str},
    )
    async def notify(args: dict[str, Any]) -> dict[str, Any]:
        ctx.notify(args["title"], args.get("body", ""))
        return _text("notified")

    @tool(
        "remember",
        "Save a durable fact to Orrin's long-term memory so it's available in future sessions. "
        "Use when the user says 'remember that…' or states a lasting preference, decision, or "
        "detail. Keep it to one concise sentence. scope='user' for facts true across all "
        "projects (a preference about you); scope='project' (default) for this project.",
        {"fact": str, "scope": str},
    )
    async def remember(args: dict[str, Any]) -> dict[str, Any]:
        added = await ctx.remember_fact(args["fact"], args.get("scope") or "project")
        return _text("remembered" if added else "already knew that")

    @tool(
        "remember_document",
        "Save a document or note into Orrin's long-term memory for THIS project, so its content "
        "is available in future sessions. Use when the user says 'remember this doc/file/note'. "
        "Pass a short descriptive title and the full text as content (if it's a file, read it "
        "first and paste the text). Orrin surfaces relevant saved notes automatically later.",
        {"title": str, "content": str},
    )
    async def remember_document(args: dict[str, Any]) -> dict[str, Any]:
        msg = await ctx.remember_document(args["title"], args["content"])
        return _text(msg)

    @tool(
        "suggest",
        "Save an ACTIONABLE suggestion or a RISK for THIS project to Orrin's suggestion queue, "
        "so the user can review and act on it later (and so it can surface in a briefing). Use "
        "this — NOT remember — for anything the user might DO: a next step, an improvement, a "
        "fix, or a risk you spotted. Keep `text` to one concrete imperative sentence. "
        "kind='action' (default) for a suggested next step; kind='risk' for a problem/hazard. "
        "severity='low' (default), 'medium', or 'high' — use 'high' only for genuinely urgent "
        "risks (data loss, security, broken build/release), which alert the user immediately. "
        "Put brief supporting context in `rationale`. Durable facts still go to `remember`.",
        {"text": str, "kind": str, "severity": str, "rationale": str},
    )
    async def suggest(args: dict[str, Any]) -> dict[str, Any]:
        msg = await ctx.suggest(args["text"], args.get("kind") or "action",
                                args.get("severity") or "low", args.get("rationale") or "")
        return _text(msg)

    @tool(
        "resolve_suggestion",
        "Close out a suggestion in Orrin's queue by its id (ids appear next to open suggestions "
        "in the overview/brief). Use status='done' when it's been handled, 'dismissed' when it "
        "no longer applies, or 'stale' if it's outdated. Do this when a background pass finds an "
        "open suggestion has already been resolved, or when the user says they've done it.",
        {"suggestion_id": str, "status": str},
    )
    async def resolve_suggestion(args: dict[str, Any]) -> dict[str, Any]:
        msg = await ctx.resolve_suggestion(args["suggestion_id"], args.get("status") or "done")
        return _text(msg)

    return create_sdk_mcp_server(
        name=SERVER_NAME,
        version="0.1.0",
        tools=[say, ask_user, render_canvas, set_status, set_mood, notify,
               remember, remember_document, suggest, resolve_suggestion],
    )


# Fully-qualified tool names, for allowed_tools / permission logic.
def qualified_names() -> list[str]:
    names = ["say", "ask_user", "render_canvas", "set_status", "set_mood", "notify",
             "remember", "remember_document", "suggest", "resolve_suggestion"]
    return [f"mcp__{SERVER_NAME}__{n}" for n in names]
