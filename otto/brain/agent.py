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
VOICE_WRAPPER = """You are the user's own Claude Code, with a voice and a screen added. The \
user TALKS to you instead of typing, and HEARS a short spoken reply; the dashboard shows this \
session's real chat — your tool calls, output, and code — exactly like a normal Claude Code \
window. Voice is just a new way to give you input; it does NOT change how you work.

DO YOUR NORMAL WORK, SHOWN IN THE CHAT. For coding, commands, files, git, debugging, or any \
real task: just do it the way you normally would in Claude Code. Your tool calls and their \
results are visible to the user in the chat window — that IS the display. Do NOT re-narrate, \
re-summarize, or re-render your work into a fancy card. Don't read commands, file contents, \
diffs, logs, or long lists out loud.

SPEAK LIKE A SHARP COLLEAGUE, BRIEFLY. Your spoken reply (your final assistant text) is read \
aloud by text-to-speech, so keep it to ONE or TWO short, natural sentences: the outcome, the \
decision, or the key number — the smart takeaway, not a play-by-play. No markdown, lists, code, \
or file paths in what you say. If the details matter, they're already on screen in the chat; \
say "it's on screen" rather than reading them.

Skip filler. Don't announce "one sec" or "let me check" before every tool — only give a brief \
heads-up if a task will genuinely take a while. Ask (with the ask_user tool, then wait) only \
when a request is truly ambiguous or a consequential action needs a yes.

render_canvas is OPTIONAL — a bonus screen, not a requirement. Use it ONLY when a visual is \
clearly better than the chat: a chart or table of data, a status dashboard, a diagram of how \
something works, a countdown/clock/weather scene, or a single big highlighted answer to a \
simple factual question. For ordinary coding and command work, do NOT call render_canvas — let \
the chat show the work. When you do render, make it self-contained HTML on a DARK theme \
(dark/transparent background, near-white text, cyan #22d3ee + purple #a855f7 accents, big \
legible type, rounded cards — never a plain white page).

Everything else about how you work — your tools, subagents, skills, project instructions — is \
unchanged."""


class ClaudeBrain:
    def __init__(self, ctx: AssistantContext, settings: Settings, *,
                 resume: str | None = None, new_chat: bool = False,
                 cwd: str | None = None) -> None:
        self.ctx = ctx
        self.settings = settings
        self.client: ClaudeSDKClient | None = None
        # Per-workspace working directory (overrides the global settings.project_path so
        # several workspaces can run in different project dirs concurrently).
        self._cwd = cwd
        # Which chat (Claude session) this brain is bound to:
        #   resume=<id> -> attach to that existing chat; new_chat=True -> a fresh chat;
        #   neither -> honor the global continue_conversation setting (today's behavior).
        self._resume = resume
        self._new_chat = new_chat
        # The live chat id — pre-seeded for a resume, else captured from the first turn.
        self.session_id: str | None = resume

    async def connect(self) -> None:
        b = self.settings.brain
        # Session binding is mutually exclusive: a specific chat (resume) OR a brand-new chat
        # OR continue-latest. Building it as a dict-spread keeps the SDK from ever getting a
        # conflicting pair (resume + continue_conversation).
        if self._new_chat:
            session_opts: dict[str, Any] = {}
        elif self._resume:
            session_opts = {"resume": self._resume}
        else:
            session_opts = {"continue_conversation": b.continue_conversation}
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
            cwd=(self._cwd or str(self.settings.project_path)),
            add_dirs=self.settings.add_dirs,
            include_partial_messages=True,
            permission_mode="default",
            **session_opts,
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
                # Capture the chat id early — this is how a brand-new chat learns its id.
                if getattr(message, "session_id", None):
                    self.session_id = message.session_id
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
                if getattr(message, "session_id", None):
                    self.session_id = message.session_id
                full = "".join(text_blocks).strip()
                if not delta_emitted and full:
                    yield {"type": "delta", "text": full}
                yield {
                    "type": "result",
                    "is_error": message.is_error,
                    "cost": message.total_cost_usd,
                    "error_text": full if message.is_error else "",
                    "session_id": self.session_id,
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
