"""SQLite system of record — searchable transcripts + structured decisions and open-tasks, on top of
the flat-file memory (otto/memory.py), which stays the source for the per-turn preamble.

This is a PURE DATA LAYER: reads and writes only. The command parsing, spoken replies, and the
"where we left off" digest live in otto/memory_commands.py — the store never composes user-facing
text. Stdlib sqlite3 (no dependency); defensive throughout — a DB error never breaks a turn.
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path
from threading import Lock

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS turns ("
    " id TEXT PRIMARY KEY, ts REAL, project TEXT, session_id TEXT, user TEXT, reply TEXT)",
    "CREATE INDEX IF NOT EXISTS turns_proj_ts ON turns(project, ts)",
    "CREATE TABLE IF NOT EXISTS decisions ("
    " id TEXT PRIMARY KEY, ts REAL, project TEXT, text TEXT, source TEXT)",
    "CREATE INDEX IF NOT EXISTS dec_proj_ts ON decisions(project, ts)",
    "CREATE TABLE IF NOT EXISTS tasks ("
    " id TEXT PRIMARY KEY, ts REAL, project TEXT, text TEXT, status TEXT, updated_ts REAL)",
    "CREATE INDEX IF NOT EXISTS task_proj_status ON tasks(project, status)",
)


def _rid() -> str:
    return uuid.uuid4().hex[:12]


class Store:
    """The durable system of record. One sqlite file at OTTO_HOME/orrin.db."""

    def __init__(self, path) -> None:
        self.path = str(path)
        self._lock = Lock()
        self.ok = False
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(self.path, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            try:
                self._db.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:
                pass
            with self._lock:
                for stmt in _SCHEMA:
                    self._db.execute(stmt)
                self._db.commit()
            self.ok = True
        except sqlite3.Error:
            self._db = None

    def _exec(self, sql: str, params: tuple = ()) -> None:
        if not self.ok:
            return
        try:
            with self._lock:
                self._db.execute(sql, params)
                self._db.commit()
        except sqlite3.Error:
            pass

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        if not self.ok:
            return []
        try:
            with self._lock:
                return list(self._db.execute(sql, params).fetchall())
        except sqlite3.Error:
            return []

    # ---- turns (searchable transcript) --------------------------------------
    def record_turn(self, project: str, user: str, reply: str, session_id: str | None = None) -> None:
        self._exec("INSERT INTO turns(id, ts, project, session_id, user, reply) VALUES (?,?,?,?,?,?)",
                   (_rid(), time.time(), project, session_id or "", user or "", reply or ""))

    def search_transcript(self, query: str, project: str | None = None, limit: int = 5) -> list[dict]:
        q = (query or "").strip()
        if not q:
            return []
        like = f"%{q}%"
        where, params = "(user LIKE ? OR reply LIKE ?)", (like, like)
        if project:
            where += " AND project = ?"
            params += (project,)
        return [dict(r) for r in self._query(
            f"SELECT ts, project, user, reply FROM turns WHERE {where} ORDER BY ts DESC LIMIT ?",
            params + (limit,))]

    def last_turn(self, project: str) -> dict | None:
        rows = self._query("SELECT ts, user, reply FROM turns WHERE project=? ORDER BY ts DESC LIMIT 1",
                           (project,))
        return dict(rows[0]) if rows else None

    # ---- decisions ----------------------------------------------------------
    def add_decision(self, project: str, text: str, source: str = "explicit") -> None:
        if (text or "").strip():
            self._exec("INSERT INTO decisions(id, ts, project, text, source) VALUES (?,?,?,?,?)",
                       (_rid(), time.time(), project, text.strip(), source))

    def recent_decisions(self, project: str, limit: int = 5) -> list[dict]:
        return [dict(r) for r in self._query(
            "SELECT ts, text, source FROM decisions WHERE project=? ORDER BY ts DESC LIMIT ?",
            (project, limit))]

    # ---- tasks --------------------------------------------------------------
    def add_task(self, project: str, text: str) -> str:
        rid, now = _rid(), time.time()
        self._exec("INSERT INTO tasks(id, ts, project, text, status, updated_ts) VALUES (?,?,?,?,?,?)",
                   (rid, now, project, (text or "").strip(), "open", now))
        return rid

    def list_tasks(self, project: str, status: str = "open", limit: int = 20) -> list[dict]:
        if status == "all":
            rows = self._query("SELECT id, ts, text, status FROM tasks WHERE project=? "
                               "ORDER BY ts DESC LIMIT ?", (project, limit))
        else:
            rows = self._query("SELECT id, ts, text, status FROM tasks WHERE project=? AND status=? "
                               "ORDER BY ts DESC LIMIT ?", (project, status, limit))
        return [dict(r) for r in rows]

    def set_task_status(self, project: str, match: str, status: str) -> dict | None:
        """Set the status of the open task matching `match` (an exact id, or a spoken fragment matched
        by substring / word-overlap). Returns the updated task, or None if nothing matched."""
        m = (match or "").strip().lower()
        if not m:
            return None
        cand = self._query("SELECT id, text FROM tasks WHERE project=? AND status='open'", (project,))
        hit = next((r for r in cand if r["id"] == match), None)
        if hit is None:
            for r in cand:
                t = (r["text"] or "").lower()
                if m in t or t in m or len(set(m.split()) & set(t.split())) >= 2:
                    hit = r
                    break
        if hit is None:
            return None
        self._exec("UPDATE tasks SET status=?, updated_ts=? WHERE id=?", (status, time.time(), hit["id"]))
        return {"id": hit["id"], "text": hit["text"], "status": status}
