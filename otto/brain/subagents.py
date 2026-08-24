"""Subagent definitions.

The main agent (Otto) dispatches these via the built-in Task tool when a job benefits
from an isolated context — e.g. a deep research sweep or a focused coding change. Each
runs with its own context window and tool set, and reports a summary back.
"""

from __future__ import annotations

from claude_agent_sdk import AgentDefinition


def default_agents() -> dict[str, AgentDefinition]:
    return {
        "researcher": AgentDefinition(
            description=(
                "Use for open-ended research: gathering and synthesizing information "
                "from the web or local files into a concise summary."
            ),
            prompt=(
                "You are a focused research subagent. Investigate the task thoroughly "
                "using web search and file reads, then return a tight, well-structured "
                "summary with the key findings and any sources. Do not chit-chat."
            ),
            tools=["WebSearch", "WebFetch", "Read", "Grep", "Glob"],
            model="sonnet",
        ),
        "coder": AgentDefinition(
            description=(
                "Use for self-contained coding tasks: implementing a change, writing a "
                "script, or fixing a bug in the workspace."
            ),
            prompt=(
                "You are a careful coding subagent. Make the requested change, keep it "
                "minimal and idiomatic, and briefly report what you changed and how to "
                "run or verify it."
            ),
            tools=["Read", "Write", "Edit", "Bash", "Grep", "Glob"],
            model="sonnet",
        ),
    }
