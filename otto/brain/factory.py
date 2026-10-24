"""Engine selection — pick the execution backend (Brain) for a workspace from config.

`resolve_engine` applies the precedence explicit → per-project (`brain.engines[cwd]`) → global
(`brain.default_engine`). `make_brain` returns `(engine_name, Brain)`: the in-process SDK
`ClaudeBrain` for an `sdk` profile, else the generic `CliAgentBrain` for any CLI tool. This is the
one place that knows about concrete brain classes; `Workspace` just calls it, so different workspaces
(focused / "meanwhile" / research) can run different engines concurrently.
"""
from __future__ import annotations

from typing import Any


def resolve_engine(settings: Any, cwd: str, engine: str | None = None) -> str:
    b = settings.brain
    name = engine or b.engines.get(cwd) or getattr(b, "default_engine", "") or "claude"
    agents = getattr(b, "agents", {}) or {}
    if name not in agents:
        name = "claude" if "claude" in agents else (next(iter(agents), "claude"))
    return name


def make_brain(ctx: Any, settings: Any, *, cwd: str, resume: str | None = None,
               new_chat: bool = False, read_only: bool = False, write_mode: bool = False,
               engine: str | None = None) -> tuple[str, Any]:
    name = resolve_engine(settings, cwd, engine)
    profile = getattr(settings.brain, "agents", {}).get(name)
    if profile is None or getattr(profile, "kind", "sdk") == "sdk":
        from .agent import ClaudeBrain
        brain = ClaudeBrain(ctx, settings, resume=resume, new_chat=new_chat, cwd=cwd,
                            read_only=read_only, write_mode=write_mode)
    else:
        from .cli_agent import CliAgentBrain
        brain = CliAgentBrain(ctx, settings, profile, resume=resume, new_chat=new_chat, cwd=cwd,
                             read_only=read_only, write_mode=write_mode)
    return name, brain
