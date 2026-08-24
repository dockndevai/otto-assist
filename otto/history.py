"""Persist the dashboard conversation so it survives reloads and restarts.

Final transcript lines and the latest canvas are appended to a JSONL file in Otto's home;
on a new dashboard connection they're replayed so the page shows the ongoing conversation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_PERSIST_TYPES = {"transcript_final", "canvas_render"}


class History:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def sink(self, event: dict[str, Any]) -> None:
        """Bus sink: append persistable events as they're published."""
        if event.get("type") not in _PERSIST_TYPES:
            return
        try:
            with self.path.open("a") as f:
                f.write(json.dumps(event) + "\n")
        except Exception:
            pass

    def load(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return the last `limit` transcript events + only the most recent canvas."""
        if not self.path.exists():
            return []
        try:
            lines = self.path.read_text().splitlines()[-limit:]
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

    def clear(self) -> None:
        try:
            self.path.unlink()
        except Exception:
            pass
