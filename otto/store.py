"""SQLite system of record — searchable transcripts + structured decisions and open-tasks, on top of
the flat-file memory (otto/memory.py), which stays the source for the per-turn preamble.

This is a PURE DATA LAYER: reads and writes only. The command parsing, spoken replies, and the
"where we left off" digest live in otto/memory_commands.py — the store never composes user-facing
text. Stdlib sqlite3 (no dependency); defensive throughout — a DB error never breaks a turn.
"""
from __future__ import annotations

import re
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
    # One row per handled turn (see otto/traces.py): route = engine | local | memory;
    # kind = focused | background | research; outcome = ok | error | auth | empty | declined;
    # feedback = 1.0 good / 0.0 bad / NULL unrated. Timings in milliseconds.
    "CREATE TABLE IF NOT EXISTS traces ("
    " id TEXT PRIMARY KEY, ts REAL, project TEXT, session_id TEXT, route TEXT, engine TEXT,"
    " kind TEXT, bucket TEXT, query TEXT, ttft_ms REAL, total_ms REAL, tools INTEGER,"
    " reply_chars INTEGER, outcome TEXT, feedback REAL)",
    "CREATE INDEX IF NOT EXISTS traces_proj_ts ON traces(project, ts)",
)

# Full-text search (FTS5 / BM25, porter-stemmed). `turns_fts` is an external-content index over
# `turns`, kept in step by triggers; `docs_fts` holds the documents the flat-file memory saved.
# Optional: if this SQLite build lacks FTS5 the store still works and search falls back to LIKE.
_FTS_SCHEMA = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5("
    " user, reply, content='turns', content_rowid='rowid', tokenize='porter unicode61')",
    "CREATE TRIGGER IF NOT EXISTS turns_fts_ai AFTER INSERT ON turns BEGIN"
    " INSERT INTO turns_fts(rowid, user, reply) VALUES (new.rowid, new.user, new.reply); END",
    "CREATE TRIGGER IF NOT EXISTS turns_fts_ad AFTER DELETE ON turns BEGIN"
    " INSERT INTO turns_fts(turns_fts, rowid, user, reply)"
    " VALUES ('delete', old.rowid, old.user, old.reply); END",
    "CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5("
    " project UNINDEXED, doc_id UNINDEXED, title, body, tokenize='porter unicode61')",
)
_FTS_VERSION = 1          # PRAGMA user_version once turns_fts has been backfilled

_WORD = re.compile(r"[A-Za-z0-9_]+")


def _match_expr(terms, op: str) -> str:
    """A safe FTS5 MATCH expression: every term double-quoted (so user text can never be parsed as
    FTS syntax), joined by AND / OR."""
    toks = [t for t in dict.fromkeys(w.lower() for term in terms for w in _WORD.findall(term or ""))]
    return f" {op} ".join(f'"{t}"' for t in toks[:16])


def _rid() -> str:
    return uuid.uuid4().hex[:12]


class Store:
    """The durable system of record. One sqlite file at OTTO_HOME/orrin.db."""

    def __init__(self, path) -> None:
        self.path = str(path)
        self._lock = Lock()
        self.ok = False
        self.fts = False
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
            return
        try:
            with self._lock:
                for stmt in _FTS_SCHEMA:
                    self._db.execute(stmt)
                if self._db.execute("PRAGMA user_version").fetchone()[0] < _FTS_VERSION:
                    # First run with FTS: index the turns recorded before it existed.
                    self._db.execute("INSERT INTO turns_fts(turns_fts) VALUES ('rebuild')")
                    self._db.execute(f"PRAGMA user_version = {_FTS_VERSION}")
                self._db.commit()
            self.fts = True
        except sqlite3.Error:
            self.fts = False

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
        if self.fts:
            # Best match first (BM25), newest breaking ties. All words first; if that finds nothing,
            # any word — so a spoken, wordy query still lands on the right exchange.
            for op in ("AND", "OR"):
                expr = _match_expr([q], op)
                if not expr:
                    break
                sql = ("SELECT t.ts, t.project, t.user, t.reply FROM turns_fts f"
                       " JOIN turns t ON t.rowid = f.rowid WHERE turns_fts MATCH ?")
                params: tuple = (expr,)
                if project:
                    sql += " AND t.project = ?"
                    params += (project,)
                rows = self._query(sql + " ORDER BY bm25(turns_fts), t.ts DESC LIMIT ?",
                                   params + (limit,))
                if rows:
                    return [dict(r) for r in rows]
                if " " not in expr:
                    break                               # single term: OR would be identical
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

    # ---- traces (one row per handled turn) -----------------------------------
    def record_trace(self, project: str, *, route: str, engine: str = "", kind: str = "focused",
                     bucket: str = "", query: str = "", ttft_ms: float | None = None,
                     total_ms: float | None = None, tools: int = 0, reply_chars: int = 0,
                     outcome: str = "ok", session_id: str | None = None) -> str:
        rid = _rid()
        self._exec(
            "INSERT INTO traces(id, ts, project, session_id, route, engine, kind, bucket, query,"
            " ttft_ms, total_ms, tools, reply_chars, outcome, feedback)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
            (rid, time.time(), project, session_id or "", route, engine, kind, bucket,
             (query or "")[:300], ttft_ms, total_ms, int(tools or 0), int(reply_chars or 0), outcome))
        return rid

    def set_feedback(self, project: str, value: float, *, max_age: float = 1800.0) -> dict | None:
        """Rate the most recent user-facing engine turn in `project` (1.0 good, 0.0 bad). Only a
        turn from the last `max_age` seconds counts, so a stray "good answer" can't re-rate
        something from yesterday. Returns the rated trace, or None if there was nothing to rate."""
        rows = self._query(
            "SELECT id, engine, bucket, query FROM traces WHERE project=? AND route='engine'"
            " AND kind='focused' AND ts >= ? ORDER BY ts DESC LIMIT 1",
            (project, time.time() - max_age))
        if not rows:
            return None
        v = max(0.0, min(1.0, float(value)))
        self._exec("UPDATE traces SET feedback=? WHERE id=?", (v, rows[0]["id"]))
        return {**dict(rows[0]), "feedback": v}

    def recent_traces(self, project: str | None = None, limit: int = 50, days: float = 0) -> list[dict]:
        where, params = [], ()
        if project:
            where.append("project = ?")
            params += (project,)
        if days and days > 0:
            where.append("ts >= ?")
            params += (time.time() - days * 86400.0,)
        sql = "SELECT * FROM traces" + (" WHERE " + " AND ".join(where) if where else "")
        return [dict(r) for r in self._query(sql + " ORDER BY ts DESC LIMIT ?", params + (limit,))]

    def trace_stats(self, project: str | None = None, days: float = 30.0,
                    min_samples: int = 5) -> dict:
        """Per-(route, engine, bucket) latency / success / feedback over the last `days`, plus the
        best-scoring engine per bucket. Research turns are left out — they aren't user requests."""
        from .traces import summarize
        rows = [r for r in self.recent_traces(project, limit=5000, days=days)
                if r.get("kind") != "research"]
        return summarize(rows, min_samples=min_samples)

    # ---- saved documents (full-text index for otto/memory.py) ----------------
    def index_doc(self, project: str, doc_id: str, title: str, body: str) -> None:
        """Add or replace one saved document in the full-text index."""
        if not (self.ok and self.fts):
            return
        try:
            with self._lock:
                self._db.execute("DELETE FROM docs_fts WHERE project=? AND doc_id=?", (project, doc_id))
                self._db.execute("INSERT INTO docs_fts(project, doc_id, title, body) VALUES (?,?,?,?)",
                                 (project, doc_id, title or "", body or ""))
                self._db.commit()
        except sqlite3.Error:
            pass

    def indexed_doc_ids(self, project: str) -> set[str]:
        return {r["doc_id"] for r in self._query("SELECT doc_id FROM docs_fts WHERE project=?", (project,))}

    def search_docs(self, project: str, terms, limit: int = 2) -> list[dict]:
        """BM25-ranked documents matching ANY of `terms` (title weighted 3x the body), each with a
        short snippet around the match."""
        expr = _match_expr(list(terms or []), "OR")
        if not (self.fts and expr):
            return []
        return [dict(r) for r in self._query(
            "SELECT doc_id, title, snippet(docs_fts, 3, '', '', '…', 48) AS snippet FROM docs_fts"
            " WHERE docs_fts MATCH ? AND project = ?"
            " ORDER BY bm25(docs_fts, 0.0, 0.0, 3.0, 1.0) LIMIT ?", (expr, project, limit))]

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
