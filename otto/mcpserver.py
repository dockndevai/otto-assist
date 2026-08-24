"""Otto exposed as an MCP server for Claude.

Mounted at /mcp on the shell's own web server, so any Claude session that runs
``claude mcp add --transport http otto http://127.0.0.1:8000/mcp`` gains a voice and a
screen. The tool handlers call the *running* Orchestrator directly (same process), so audio
and the dashboard just work.
"""

from __future__ import annotations

from fastmcp import FastMCP


def build_mcp(orch):
    mcp = FastMCP("otto")

    @mcp.tool
    async def speak(text: str) -> str:
        """Speak text aloud through Otto's voice."""
        await orch.say(text)
        return "spoken"

    @mcp.tool
    async def ask(question: str) -> str:
        """Ask the user a question aloud and return their spoken or typed answer."""
        return await orch.ask_user(question)

    @mcp.tool
    async def show(content: str, format: str = "html") -> str:
        """Show HTML or Markdown on Otto's dashboard canvas."""
        orch.render_canvas(content, format)
        return "shown"

    @mcp.tool
    async def notify(title: str, body: str = "") -> str:
        """Post a macOS desktop notification through Otto."""
        orch.notify(title, body)
        return "notified"

    @mcp.tool
    def status() -> str:
        """Otto's current state and active project."""
        return f"{orch.state} · project={orch.current_project}"

    return mcp
