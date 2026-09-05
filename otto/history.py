"""Persist the dashboard conversation so it survives reloads and restarts — per project.

Final transcript lines and the latest canvas are appended to a per-project JSONL file inside
the memory store (``memory/projects/<slug>/history.jsonl``), so a reopened dashboard replays
the history of the project it's focused on — matching the rest of what Orrin remembers. Events
carry a ``ws`` (workspace = project path) tag; anything untagged falls back to the focused
project via ``resolve_project``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

_PERSIST_TYPES = {"transcript_final", "canvas_render"}


class History:
    def __init__(self, memory: Any, resolve_project: Callable[[], str]) -> None:
        self.memory = memory
        self.resolve_project = resolve_project

    def _path(self, project: str) -> Path:
        return self.memory.history_path(project)

    def sink(self, event: dict[str, Any]) -> None:
        """Bus sink: append persistable events to their project's history as they publish."""
        if event.get("type") not in _PERSIST_TYPES:
            return
        project = event.get("ws") or self.resolve_project()
        try:
            with self._path(project).open("a") as f:
                f.write(json.dumps(event) + "\n")
        except Exception:
            pass

    def load(self, project: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """Return the last `limit` transcript events + only the most recent canvas, for a
        project (defaults to the focused one)."""
        project = project or self.resolve_project()
        path = self._path(project)
        if not path.exists():
            return []
        try:
            lines = path.read_text().splitlines()[-limit:]
        except Exception:
            return []
        out: list[dict[str, Any]] = []
        last_canvas: dict[str, Any] | None = None
        for ln in lines:
            try:
                ev = json.loads(ln)
            except Exception:
                continue
            if ev.get("type") == "canvas_render":
                last_canvas = ev
                continue
            out.append(ev)
        if last_canvas:
            out.append(last_canvas)
        return out

    def clear(self, project: str | None = None) -> None:
        project = project or self.resolve_project()
        try:
            self._path(project).unlink()
        except Exception:
            pass

    def migrate_legacy(self, old_path: str | Path, default_project: str) -> None:
        """One-time: split a pre-existing flat history.jsonl into per-project files, then set it
        aside so it isn't re-imported. Untagged events go to `default_project`."""
        old = Path(old_path)
        if not old.exists():
            return
        try:
            lines = old.read_text().splitlines()
        except Exception:
            return
        buckets: dict[str, list[str]] = {}
        for ln in lines:
            try:
                ev = json.loads(ln)
            except Exception:
                continue
            proj = ev.get("ws") or default_project
            buckets.setdefault(proj, []).append(ln)
        for proj, rows in buckets.items():
            try:
                with self._path(proj).open("a") as f:
                    f.write("\n".join(rows) + "\n")
            except Exception:
                pass
        try:
            old.rename(old.with_suffix(".jsonl.migrated"))
        except Exception:
            pass
