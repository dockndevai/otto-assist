"""The brain: a persistent Claude session wired to Jarvis's tools and subagents.

Wraps ClaudeSDKClient. `send()` streams the response back as a series of small events
(text deltas, tool_use, result) that the orchestrator forwards to the mouth (TTS) and the
screen (web UI).
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
)

from ..config import Settings
from .subagents import default_agents
from .tools import AssistantContext, build_server

PERSONA = """You are Jarvis, a realtime personal assistant that talks with the user by voice \
and shows things on a dashboard. You run on the user's own Mac and can get real work done.

Style:
- Your replies are SPOKEN aloud, so keep them short, natural, and conversational. No \
markdown, no bullet lists, no code blocks in spoken replies.
- When you have something visual or long to present (a chart, a table, a document, code, \
detailed results), DON'T read it aloud — call the render_canvas tool to show it on the \
dashboard, and say a one-line summary out loud.
- When a request is ambiguous or you need a decision, use the ask_user tool and wait for \
the answer. Don't guess on consequential things.
- Before a consequential or irreversible action (sending messages, deleting, spending), \
confirm with the user first.

Capabilities:
- You can read/write files and run shell commands in your workspace directory.
- For open-ended research or self-contained coding jobs, dispatch a subagent with the \
Task tool (researcher / coder). Tell the user briefly when you're kicking one off.
- Use the say tool to give a quick spoken progress update during longer work.

Be proactive, concise, and genuinely useful."""

# Built-in tools that mutate the machine — gated when permission mode is "ask".
ASK_TOOLS = {"Bash", "Write", "Edit", "MultiEdit", "NotebookEdit"}


def _describe(tool_name: str, args: dict[str, Any]) -> str:
    """A short human phrase for a pending action, for spoken/typed confirmation."""
    if tool_name == "Bash":
        return f"run the command: {args.get('command', '')}"
    if tool_name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return f"modify the file {args.get('file_path', args.get('path', 'a file'))}"
    return f"use the {tool_name} tool"


class Brain:
    def __init__(self, ctx: AssistantContext, settings: Settings) -> None:
        self.ctx = ctx
        self.settings = settings
        self.client: ClaudeSDKClient | None = None

    async def connect(self) -> None:
        server = build_server(self.ctx)
        options = ClaudeAgentOptions(
            system_prompt=PERSONA,
            model=self.settings.brain.model,
            mcp_servers={"jarvis": server},
            agents=default_agents(),
            can_use_tool=self._permission,
            cwd=str(self.settings.workspace_path),
            setting_sources=[],  # isolated — ignore ambient project/user settings
            include_partial_messages=True,
            permission_mode="default",
        )
        self.client = ClaudeSDKClient(options=options)
        await self.client.connect()

    async def disconnect(self) -> None:
        if self.client:
            await self.client.disconnect()

    async def interrupt(self) -> None:
        if self.client:
            try:
                await self.client.interrupt()
            except Exception:
                pass

    async def _permission(
        self, tool_name: str, input_data: dict[str, Any], context: Any
    ):
        if tool_name in ASK_TOOLS and self.settings.brain.permission == "ask":
            ok = await self.ctx.confirm(_describe(tool_name, input_data))
            if not ok:
                return PermissionResultDeny(message="The user declined this action.")
        return PermissionResultAllow()

    async def send(self, text: str) -> AsyncIterator[dict[str, Any]]:
        """Send a user turn; yield {type: delta|tool_use|result} events."""
        assert self.client is not None, "Brain not connected"
        await self.client.query(text)
        delta_emitted = False
        text_blocks: list[str] = []
        async for message in self.client.receive_response():
            if isinstance(message, StreamEvent):
                delta = _extract_text_delta(message.event)
                if delta:
                    delta_emitted = True
                    yield {"type": "delta", "text": delta}
            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text_blocks.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        yield {
                            "type": "tool_use",
                            "name": block.name,
                            "detail": _tool_detail(block.name, block.input),
                        }
            elif isinstance(message, ResultMessage):
                full = "".join(text_blocks).strip()
                # Fallback: if the CLI didn't stream token deltas, emit the whole text.
                if not delta_emitted and full:
                    yield {"type": "delta", "text": full}
                yield {
                    "type": "result",
                    "is_error": message.is_error,
                    "cost": message.total_cost_usd,
                    "error_text": full if message.is_error else "",
                }


def _extract_text_delta(event: dict[str, Any] | None) -> str:
    if not isinstance(event, dict):
        return ""
    if event.get("type") == "content_block_delta":
        delta = event.get("delta") or {}
        if delta.get("type") == "text_delta":
            return delta.get("text", "")
    return ""


def _tool_detail(name: str, args: dict[str, Any]) -> str:
    if name == "Task":
        return f"{args.get('subagent_type', 'subagent')}: {args.get('description', '')}"
    if name == "Bash":
        return str(args.get("command", ""))[:120]
    if name.startswith("mcp__jarvis__"):
        return ""
    return ", ".join(f"{k}={str(v)[:40]}" for k, v in list(args.items())[:2])
