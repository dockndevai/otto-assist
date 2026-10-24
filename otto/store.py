"""SQLite system of record — searchable transcripts + structured decisions / open-tasks /
preferences, and a "continue where we left off" digest.

Additive and non-breaking: the flat-file memory (otto/memory.py) remains the source for the per-turn
preamble and for what the brain reads; this layer adds queryable, time-scoped, TYPED records on top,
so Orrin can answer "what are my open tasks", "what did we decide about X", and "continue where we
left off yesterday". Stdlib sqlite3 only (no new dependency). Every method is defensive — a DB error
never breaks a turn.
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
    " id TEXT PRIMARY KEY, ts REAL, project TEXT, text TEXT, rationale TEXT, source TEXT)",
    "CREATE INDEX IF NOT EXISTS dec_proj_ts ON decisions(project, ts)",
    "CREATE TABLE IF NOT EXISTS tasks ("
    " id TEXT PRIMARY KEY, ts REAL, project TEXT, text TEXT, status TEXT, updated_ts REAL)",
    "CREATE INDEX IF NOT EXISTS task_proj_status ON tasks(project, status)",
    "CREATE TABLE IF NOT EXISTS preferences ("
    " id TEXT PRIMARY KEY, ts REAL, scope TEXT, text TEXT)",
)


def _rid() -> str:
    return uuid.uuid4().hex[:12]


def _ago(ts: float) -> str:
    """Human 'how long ago' for a digest line."""
    secs = max(0, int(time.time() - ts))
    if secs < 3600:
        return "just now" if secs < 90 else f"{secs // 60} min ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    d = secs // 86400
    return "yesterday" if d == 1 else f"{d} days ago"


class Store:
    """The durable system of record. One sqlite file at OTTO_HOME/orrin.db."""

    def __init__(self, path) -> None:
        self.path = str(path)
        self._lock = Lock()
        self.ok = False
        self._fts = False
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(self.path, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            try:
                self._db.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:
                pass
            self._init_schema()
            self.ok = True
        except sqlite3.Error:
            self._db = None

    def _init_schema(self) -> None:
        with self._lock:
            for stmt in _SCHEMA:
                self._db.execute(stmt)
            try:                                   # full-text search is optional
                self._db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts "
                                 "USING fts5(id UNINDEXED, project UNINDEXED, text)")
                self._fts = True
            except sqlite3.OperationalError:
                self._fts = False                  # FTS5 not compiled in → LIKE fallback
            self._db.commit()

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
        rid = _rid()
        self._exec("INSERT INTO turns(id, ts, project, session_id, user, reply) VALUES (?,?,?,?,?,?)",
                   (rid, time.time(), project, session_id or "", user or "", reply or ""))
        if self._fts:
            self._exec("INSERT INTO turns_fts(id, project, text) VALUES (?,?,?)",
                       (rid, project, f"{user or ''}\n{reply or ''}"))

    def search_transcript(self, query: str, project: str | None = None, limit: int = 5) -> list[dict]:
        q = (query or "").strip()
        if not q:
            return []
        rows: list[sqlite3.Row] = []
        if self._fts:
            # match FTS, then join back for the full turn
            where = "turns_fts MATCH ?"
            params: tuple = (q,)
            if project:
                where += " AND turns_fts.project = ?"
                params += (project,)
            rows = self._query(
                "SELECT t.ts, t.project, t.user, t.reply FROM turns_fts "
                "JOIN turns t ON t.id = turns_fts.id "
                f"WHERE {where} ORDER BY t.ts DESC LIMIT ?", params + (limit,))
        if not rows:                               # LIKE fallback (or FTS found nothing)
            like = f"%{q}%"
            where = "(user LIKE ? OR reply LIKE ?)"
            params = (like, like)
            if project:
                where += " AND project = ?"
                params += (project,)
            rows = self._query(
                f"SELECT ts, project, user, reply FROM turns WHERE {where} "
                "ORDER BY ts DESC LIMIT ?", params + (limit,))
        return [dict(r) for r in rows]

    def last_turn(self, project: str) -> dict | None:
        rows = self._query("SELECT ts, user, reply FROM turns WHERE project=? ORDER BY ts DESC LIMIT 1",
                           (project,))
        return dict(rows[0]) if rows else None

    # ---- decisions ----------------------------------------------------------
    def add_decision(self, project: str, text: str, rationale: str = "", source: str = "explicit") -> None:
        if not (text or "").strip():
            return
        self._exec("INSERT INTO decisions(id, ts, project, text, rationale, source) VALUES (?,?,?,?,?,?)",
                   (_rid(), time.time(), project, text.strip(), (rationale or "").strip(), source))

    def recent_decisions(self, project: str, limit: int = 5) -> list[dict]:
        return [dict(r) for r in self._query(
            "SELECT ts, text, rationale, source FROM decisions WHERE project=? ORDER BY ts DESC LIMIT ?",
            (project, limit))]

    # ---- tasks --------------------------------------------------------------
    def add_task(self, project: str, text: str) -> str:
        rid = _rid()
        now = time.time()
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
        """Set the status of the open task whose text best matches `match` (a spoken fragment), or an
        exact task id. Returns the updated task, or None if nothing matched."""
        m = (match or "").strip().lower()
        if not m:
            return None
        cand = self._query("SELECT id, text FROM tasks WHERE project=? AND status='open'", (project,))
        hit = None
        for r in cand:
            if r["id"] == match:
                hit = r
                break
        if hit is None:
            for r in cand:                         # substring / word-overlap match
                t = (r["text"] or "").lower()
                if m in t or t in m or len(set(m.split()) & set(t.split())) >= 2:
                    hit = r
                    break
        if hit is None:
            return None
        self._exec("UPDATE tasks SET status=?, updated_ts=? WHERE id=?",
                   (status, time.time(), hit["id"]))
        return {"id": hit["id"], "text": hit["text"], "status": status}

    # ---- preferences --------------------------------------------------------
    def add_preference(self, text: str, scope: str = "user") -> None:
        if not (text or "").strip():
            return
        self._exec("INSERT INTO preferences(id, ts, scope, text) VALUES (?,?,?,?)",
                   (_rid(), time.time(), scope, text.strip()))

    # ---- resume digest ------------------------------------------------------
    def resume_digest(self, project: str, *, max_tasks: int = 5, max_decisions: int = 3) -> str:
        """A short 'where we left off' block: when we last spoke, the last topic, recent decisions,
        and open tasks. Empty string when there's nothing to resume."""
        last = self.last_turn(project)
        tasks = self.list_tasks(project, "open", max_tasks)
        decs = self.recent_decisions(project, max_decisions)
        if not (last or tasks or decs):
            return ""
        lines = []
        if last:
            topic = (last.get("user") or "").strip().replace("\n", " ")
            if len(topic) > 90:
                topic = topic[:88] + "…"
            lines.append(f"Last time ({_ago(last['ts'])}) you said: “{topic}”")
        if decs:
            lines.append("Decisions: " + "; ".join(
                (d["text"][:80] + ("…" if len(d["text"]) > 80 else "")) for d in decs))
        if tasks:
            lines.append("Open tasks: " + "; ".join(
                (t["text"][:70] + ("…" if len(t["text"]) > 70 else "")) for t in tasks))
        return "\n".join(lines)

    def close(self) -> None:
        if self.ok and self._db is not None:
            try:
                self._db.close()
            except sqlite3.Error:
                pass
