"""Chat (session) discovery for a project — the "chats" switcher.

A Claude Code *chat* is a session stored at ``~/.claude/projects/<encoded>/<sessionId>.jsonl``.
The Agent SDK already enumerates them (``list_sessions``) and can rename one
(``rename_session``); this module is a thin, SDK-isolating wrapper so the orchestrator and
server never import the SDK directly. Mirrors the shape/conventions of ``otto/projects.py``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Session:
    session_id: str
    title: str                 # human label: custom title -> summary -> first prompt -> id[:8]
    first_prompt: str          # short preview of the opening message (fallback label)
    updated_at: float          # epoch seconds (last modified), for sorting + relative time
    git_branch: str | None = None
    current: bool = False       # is this the brain's active chat?


def list_sessions(project_dir: str, current_id: str | None = None,
                  limit: int = 30) -> list[Session]:
    """The chats in ``project_dir``, most-recently-active first. ``[]`` on any error.

    Uses the SDK's ``list_sessions(directory=...)`` (worktrees excluded — Otto keeps one
    active chat, no parallel sessions). Titles prefer a user-set custom title, then the
    SDK summary, then the first prompt, then a short id.
    """
    try:
        from claude_agent_sdk import list_sessions as _sdk_list_sessions
        infos = _sdk_list_sessions(directory=str(project_dir), limit=limit,
                                   include_worktrees=False)
    except Exception:
        return []
    out: list[Session] = []
    for info in infos:
        sid = getattr(info, "session_id", "") or ""
        if not sid:
            continue
        title = (getattr(info, "custom_title", None) or getattr(info, "summary", None)
                 or getattr(info, "first_prompt", None) or sid[:8])
        out.append(Session(
            session_id=sid,
            title=(str(title).strip()[:80] or sid[:8]),
            first_prompt=(getattr(info, "first_prompt", None) or "").strip()[:120],
            updated_at=float(getattr(info, "last_modified", 0) or 0),
            git_branch=getattr(info, "git_branch", None),
            current=(sid == current_id),
        ))
    out.sort(key=lambda s: s.updated_at, reverse=True)
    return out


def rename_session_title(project_dir: str, session_id: str, title: str) -> None:
    """Set a chat's display title (appends a custom-title entry; latest wins)."""
    from claude_agent_sdk import rename_session
    rename_session(session_id, title, directory=str(project_dir))
