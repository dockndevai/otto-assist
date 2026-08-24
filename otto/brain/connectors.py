"""Agent connectors — how Otto reaches other agents/tools on the machine.

Two kinds, both declared in config.yaml under `connectors`:

- **mcp**: an external MCP server (stdio/http/sse). Its tools are attached directly to
  Otto, so the brain can call them like any other tool. Great for anything MCP-native
  (filesystem, github, Claude-Code-as-MCP, custom servers).

- **cli**: any command-line agent (e.g. `claude -p`, `aider`, a custom script). Exposed
  to Otto as a single `delegate` tool: Otto hands it a task string, we run the command,
  and return its stdout. Great for headless agents that take a prompt and print a result.

This is the foundation of the multi-agent orchestrator: add an entry to config and Otto
can route work to it — no code changes.
"""

from __future__ import annotations

import asyncio
import shlex
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from ..config import Settings

DELEGATE_SERVER = "agents"


def external_mcp_servers(settings: Settings) -> dict[str, dict[str, Any]]:
    """Map configured MCP connectors to the SDK's mcp_servers config dicts."""
    out: dict[str, dict[str, Any]] = {}
    for name, c in settings.connectors.mcp.items():
        if c.transport == "stdio":
            if not c.command:
                continue
            cfg: dict[str, Any] = {"type": "stdio", "command": c.command}
            if c.args:
                cfg["args"] = c.args
            if c.env:
                cfg["env"] = c.env
        else:  # http / sse
            if not c.url:
                continue
            cfg = {"type": c.transport, "url": c.url}
            if c.headers:
                cfg["headers"] = c.headers
        out[name] = cfg
    return out


def cli_agents_description(settings: Settings) -> str:
    """A human-readable list of CLI agents, for the system prompt."""
    lines = []
    for name, c in settings.connectors.cli.items():
        desc = f" — {c.description}" if c.description else ""
        lines.append(f'  • "{name}"{desc}')
    return "\n".join(lines)


async def run_cli(conn, task: str, default_cwd: str) -> str:
    """Run one CLI connector with `task`, returning a labelled result string."""
    # Build argv: substitute {task} if present, else append the task.
    argv = [task if a == "{task}" else a for a in conn.command]
    if "{task}" not in conn.command:
        argv.append(task)
    cwd = conn.cwd or default_cwd
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=conn.timeout)
        text = (out or b"").decode("utf-8", "replace").strip()
        status = "ok" if proc.returncode == 0 else f"exit {proc.returncode}"
        return f"[{status}]\n{text[:8000]}" if text else f"[{status}] (no output)"
    except asyncio.TimeoutError:
        return f"timed out after {conn.timeout}s (command: {shlex.join(argv)})"
    except FileNotFoundError:
        return f"command not found: {argv[0]}"
    except Exception as e:  # noqa: BLE001
        return f"failed: {type(e).__name__}: {e}"


def build_delegate_server(settings: Settings):
    """In-process MCP server exposing `delegate` (run a configured CLI agent).

    Returns None if no CLI connectors are configured.
    """
    if not settings.connectors.cli:
        return None

    @tool(
        "delegate",
        "Hand a task to one of your configured command-line agents and get its output. "
        "Use the exact agent name from your list of available agents.",
        {"agent": str, "task": str},
    )
    async def delegate(args: dict[str, Any]) -> dict[str, Any]:
        name = args.get("agent", "")
        conn = settings.connectors.cli.get(name)
        if conn is None:
            avail = ", ".join(settings.connectors.cli) or "(none configured)"
            return _text(f"No agent named '{name}'. Available: {avail}")
        result = await run_cli(conn, args.get("task", ""), str(settings.workspace_path))
        return _text(f"[{name}] {result}")

    return create_sdk_mcp_server(name=DELEGATE_SERVER, version="0.1.0", tools=[delegate])


def _text(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}]}
