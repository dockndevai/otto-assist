"""Project discovery for the menu-bar switcher.

Claude Code stores conversations per working directory under ``~/.claude/projects/`` with
the path encoded as ``/`` -> ``-``. We decode those back into real directories so Otto can
offer your recent projects, merged with any you've pinned in config.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"


@dataclass
class Project:
    path: str          # absolute directory
    name: str          # basename, for display
    current: bool = False


def _decode(encoded: str) -> str | None:
    """Turn a ~/.claude/projects dir name back into a filesystem path.

    Claude encodes the cwd as ``/`` -> ``-``, which is ambiguous for path segments that
    themselves contain ``-`` (e.g. ``personal-assist``). We resolve the ambiguity against
    the real filesystem: at each step, greedily extend the current segment with following
    tokens until the joined path exists.
    """
    if not encoded.startswith("-"):
        return None
    tokens = encoded[1:].split("-")
    path = Path("/")
    i = 0
    while i < len(tokens):
        seg = tokens[i]
        i += 1
        while not (path / seg).exists() and i < len(tokens):
            seg = f"{seg}-{tokens[i]}"
            i += 1
        path = path / seg
        if not path.exists():
            return None
    return str(path) if path.is_dir() else None


def discover_recent(limit: int = 12) -> list[str]:
    """Recent Claude project directories, most-recently-modified first."""
    if not CLAUDE_PROJECTS.is_dir():
        return []
    entries = []
    for d in CLAUDE_PROJECTS.iterdir():
        if not d.is_dir():
            continue
        path = _decode(d.name)
        if path:
            entries.append((d.stat().st_mtime, path))
    entries.sort(reverse=True)
    return [p for _, p in entries[:limit]]


def list_projects(current: str | None = None, pinned: list[str] | None = None,
                  limit: int = 12) -> list[Project]:
    """Pinned projects first, then recent Claude projects; de-duplicated, current marked."""
    cur = str(Path(current).expanduser()) if current else None
    seen: set[str] = set()
    out: list[Project] = []

    def add(path: str) -> None:
        ap = str(Path(path).expanduser())
        if ap in seen or not Path(ap).is_dir():
            return
        seen.add(ap)
        out.append(Project(path=ap, name=Path(ap).name or ap, current=(ap == cur)))

    for p in (pinned or []):
        add(p)
    for p in discover_recent(limit):
        add(p)
    if cur and cur not in seen:       # always include the current project
        out.insert(0, Project(path=cur, name=Path(cur).name or cur, current=True))
    return out
