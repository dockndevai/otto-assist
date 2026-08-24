"""ClaudeBrain — runs the user's own Claude, wired to Otto's voice/screen.

This is deliberately thin. It does NOT define a persona, subagents, or MCP servers of its
own. Instead it loads the user's Claude configuration (settings, MCP servers, subagents,
skills, CLAUDE.md, permissions) via ``setting_sources`` and uses the ``claude_code`` system
prompt preset — so Otto behaves exactly like the user's Claude, just spoken. The only thing
Otto injects is a short voice-manners note and its voice/screen tools.
"""

from __future__ import annotations

import os
import re
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
tools, subagents, project instructions — is unchanged.

Be fast. Answer simple questions (math, general knowledge, quick chat) directly from what \
you know, without tools. Only reach for a tool when you genuinely need live/external data \
(current time, email, files, the web). Prefer one tool call over many. Keep spoken answers \
to a sentence or two.

Acknowledge first, then act. Whenever a request needs a tool that takes a moment (checking \
email, files, GitHub, the web, running a command), FIRST say a brief spoken acknowledgment \
like "Sure, let me check that" or "One sec, pulling that up" — then call the tool — then \
give the answer. Never go silent straight into a tool call; the user should always hear you \
acknowledge before you start working."""


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
        # Read-only tools (searching Gmail, listing files, web search…) never need a
        # confirmation — only consequential actions do.
        if _is_read_only(tool_name):
            return PermissionResultAllow()
        if tool_name == "Bash" and _bash_is_read_only(input_data.get("command", "")):
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


# Built-in tools that only read; and verb patterns for classifying MCP tools.
_READ_BUILTINS = {"Read", "Grep", "Glob", "LS", "WebSearch", "WebFetch", "TodoWrite",
                  "NotebookRead", "Task"}
_WRITE_VERBS = re.compile(
    r"(send|create|delete|remove|update|write|edit|trash|move|archive|reply|forward|"
    r"draft|post|add|set|modify|label|insert|upload|share|rename|revoke|cancel|pay|buy)",
    re.I)
_READ_VERBS = re.compile(
    r"(search|list|get|read|fetch|view|find|query|status|count|show|describe|lookup|"
    r"recent|history|threads|messages|events|summary|details)", re.I)


# Shell commands that only read state — safe to run without a spoken confirmation.
_BASH_READ_CMDS = {"date", "ls", "pwd", "cat", "head", "tail", "echo", "whoami", "df",
                   "du", "uptime", "cal", "which", "type", "printenv", "hostname", "uname",
                   "wc", "find", "grep", "rg", "stat", "file", "open"}
_BASH_DANGER = re.compile(r"([|;&><`$]|\brm\b|\bsudo\b|\bmv\b|\bcp\b|\bkill\b|\bdd\b|"
                          r"\bchmod\b|\bchown\b|\bcurl\b|\bwget\b|\bgit\s+(push|commit|reset)|"
                          r"\bgh\s+\w+\s+(create|delete|edit|merge|close))")


def _bash_is_read_only(command: str) -> bool:
    cmd = command.strip()
    if not cmd or _BASH_DANGER.search(cmd):
        return False
    first = cmd.split()[0] if cmd.split() else ""
    if first in _BASH_READ_CMDS:
        return True
    # read-only subcommands of common tools
    if re.match(r"^(git\s+(status|log|diff|show|branch)|gh\s+\w+\s+(list|view|status))", cmd):
        return True
    return False


def _is_read_only(tool_name: str) -> bool:
    """Best-effort: does this tool only read? Reads skip the confirmation prompt."""
    if tool_name in _READ_BUILTINS:
        return True
    if tool_name.startswith("mcp__"):
        leaf = tool_name.split("__")[-1]
        if _WRITE_VERBS.search(leaf):     # a write verb wins (e.g. send_message)
            return False
        if _READ_VERBS.search(leaf):
            return True
    return False


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
