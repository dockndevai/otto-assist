"""ClaudeBrain — runs the user's own Claude, wired to Otto's voice/screen.

This is deliberately thin. It does NOT define a persona, subagents, or MCP servers of its
own. Instead it loads the user's Claude configuration (settings, MCP servers, subagents,
skills, CLAUDE.md, permissions) via ``setting_sources`` and uses the ``claude_code`` system
prompt preset — so Otto behaves exactly like the user's Claude, just spoken. The only thing
Otto injects is a short voice-manners note and its voice/screen tools.
"""

from __future__ import annotations

import os
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
from .tools import AssistantContext, build_server

# Appended to Claude's own system prompt — voice manners only, no persona override.
VOICE_WRAPPER = """You are being spoken to and heard aloud through Otto, a voice interface.
Keep replies short, natural, and conversational — they are read out by text-to-speech, so \
no markdown, bullet lists, or code blocks in what you say. When you have something visual \
or long to present (a chart, table, document, code, detailed results), don't read it out — \
call the render_canvas tool to show it on the dashboard and say a one-line summary. When a \
request is ambiguous or needs a decision, use the ask_user tool and wait. Use the say tool \
for quick progress updates during longer work. Everything else about how you work — your \
tools, subagents, project instructions — is unchanged."""


class ClaudeBrain:
    def __init__(self, ctx: AssistantContext, settings: Settings) -> None:
        self.ctx = ctx
        self.settings = settings
        self.client: ClaudeSDKClient | None = None

    async def connect(self) -> None:
        b = self.settings.brain
        options = ClaudeAgentOptions(
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": VOICE_WRAPPER,
            },
            # Load the user's own Claude setup: MCP servers, subagents, skills, CLAUDE.md.
            setting_sources=(["user", "project", "local"] if b.use_claude_config else []),
            # Otto's voice/screen tools are the only thing we add (named "voice" to avoid
            # colliding with an externally-registered "otto" MCP the user's Claude may load).
            mcp_servers={"voice": build_server(self.ctx)},
            can_use_tool=self._permission,
            cwd=str(self.settings.project_path),
            add_dirs=self.settings.add_dirs,
            continue_conversation=b.continue_conversation,
            include_partial_messages=True,
            permission_mode="default",
            **({"model": b.model} if b.model else {}),
        )
        self.client = ClaudeSDKClient(options=options)
        await self.client.connect()

    async def disconnect(self) -> None:
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None

    async def interrupt(self) -> None:
        if self.client:
            try:
                await self.client.interrupt()
            except Exception:
                pass

    async def _permission(self, tool_name: str, input_data: dict[str, Any], context: Any):
        """Route Claude's permission prompts to voice/UI per the wiring setting.

        Only invoked for tools the user's own Claude rules didn't already auto-approve.
        Otto's own voice/screen tools are always allowed.
        """
        if tool_name.startswith("mcp__voice__"):
            return PermissionResultAllow()
        mode = self.settings.brain.permission
        if mode == "auto":
            return PermissionResultAllow()
        if mode == "deny":
            return PermissionResultDeny(message="Denied by Otto's permission setting.")
        ok = await self.ctx.confirm(_describe(tool_name, input_data))
        return PermissionResultAllow() if ok else PermissionResultDeny(
            message="The user declined this action."
        )

    async def send(self, text: str) -> AsyncIterator[dict[str, Any]]:
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
                if not delta_emitted and full:
                    yield {"type": "delta", "text": full}
                yield {
                    "type": "result",
                    "is_error": message.is_error,
                    "cost": message.total_cost_usd,
                    "error_text": full if message.is_error else "",
                }


def _describe(tool_name: str, args: dict[str, Any]) -> str:
    if tool_name == "Bash":
        return f"run the command: {args.get('command', '')}"
    if tool_name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return f"modify the file {args.get('file_path', args.get('path', 'a file'))}"
    return f"use the {tool_name} tool"


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
    if name.startswith("mcp__voice__"):
        return ""
    return ", ".join(f"{k}={str(v)[:40]}" for k, v in list(args.items())[:2])
