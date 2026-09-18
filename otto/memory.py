"""Orrin's persistent, cross-session memory.

Everything Orrin learns lives in a single per-user directory (``OTTO_HOME/memory``) that is
shared by *every* Orrin session, so knowledge carries over from one conversation to the next.
It's organized per project — because Orrin works inside projects — plus a global tier for
facts about the user that span all projects::

    OTTO_HOME/memory/
      global.md                 # durable facts about the user (all projects)
      index.json                # registry: slug -> {path, name, last_active, turns}
      projects/<slug>/
        project.md              # curated knowledge about this project
        summary.md              # rolling one-line summaries of recent conversations
        turns.jsonl             # durable raw log of every exchange (user + reply)

Two mechanisms make this feel like memory rather than just storage:

* **Injection** — before each turn, :meth:`Memory.preamble` composes a *bounded* block
  (global facts + this project's facts + the recent summary) that the orchestrator prepends
  to the user's message, so Claude always has the durable context in view without us trying
  to stuff an unbounded transcript into the window.
* **Capture** — after each turn, :meth:`Memory.capture` records the exchange and (fully
  automatically) runs a cheap model pass that decides what's worth remembering long-term and
  folds it into ``global.md`` / ``project.md`` / ``summary.md``.

The full turn-by-turn transcript of a chat is still owned by Claude Code itself (Orrin resumes
the project's session); this store adds the curated, cross-session, cross-project layer on top.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .config import Settings

_FENCE = re.compile(r"^```[a-zA-Z]*\n?|\n?```$")

# Common words dropped from document keyword indexing / query matching (keeps retrieval on
# content words). Deliberately small and dependency-free.
_STOP = {
    "the", "and", "for", "you", "your", "this", "that", "with", "was", "are", "but", "not",
    "have", "has", "had", "from", "they", "them", "then", "there", "here", "what", "when",
    "how", "why", "who", "can", "could", "would", "should", "will", "into", "about", "out",
    "our", "his", "her", "its", "their", "which", "were", "been", "being", "does", "did",
    "get", "got", "any", "all", "some", "just", "like", "one", "two", "use", "used", "also",
}


_GREETING = re.compile(
    r"^(hi|hey|hello|yo|thanks|thank you|ok|okay|k|got it|cool|nice|great|awesome|"
    r"yes|no|yep|nope|sure|good (morning|afternoon|evening)|bye|goodbye)[\s!.?,]*$", re.I)


def project_slug(project_path: str) -> str:
    """Stable, human-readable directory name for a project path.

    Mirrors Claude Code's own encoding of ``~/.claude/projects`` (path with the separators
    turned into dashes) so the two are easy to line up by eye.
    """
    p = str(Path(project_path).expanduser())
    slug = re.sub(r"[^A-Za-z0-9]+", "-", p).strip("-")
    return slug or "root"


def _normalize(line: str) -> str:
    return re.sub(r"\s+", " ", line.lstrip("-* ").strip()).lower()


class Memory:
    def __init__(self, root: str | Path, settings: Settings) -> None:
        self.root = Path(root)
        self.settings = settings
        self._locks: dict[str, asyncio.Lock] = {}
        self._capturing: set[str] = set()   # projects with an extract in flight (avoid pile-up)
        try:
            (self.root / "projects").mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    # -- paths -------------------------------------------------------------
    @property
    def _cfg(self):
        return self.settings.memory

    @property
    def global_path(self) -> Path:
        return self.root / "global.md"

    def project_dir(self, project_path: str) -> Path:
        d = self.root / "projects" / project_slug(project_path)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def history_path(self, project_path: str) -> Path:
        """Per-project dashboard history (co-located with the rest of the store, so replay
        matches what's remembered)."""
        return self.project_dir(project_path) / "history.jsonl"

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    # -- reads -------------------------------------------------------------
    @staticmethod
    def _read(path: Path) -> str:
        try:
            return path.read_text()
        except Exception:
            return ""

    @staticmethod
    def _bullets(text: str) -> list[str]:
        """The '- ' bullet lines from a markdown body (drops headers/blanks)."""
        return [ln.strip() for ln in text.splitlines()
                if ln.strip().startswith("- ") or ln.strip().startswith("* ")]

    def preamble(self, project_path: str, query: str = "") -> str:
        """A bounded memory block to prepend to a turn. Empty string when there's nothing
        remembered yet (so a fresh install adds no noise). ``query`` (the user's turn) is used
        to pull in the most relevant snippets from saved documents."""
        if not (self._cfg.enabled and self._cfg.inject):
            return ""
        pdir = self.root / "projects" / project_slug(project_path)
        name = str(project_path).rstrip("/").split("/")[-1] or project_path
        g = "\n".join(self._bullets(self._read(self.global_path)))
        pj = "\n".join(self._bullets(self._read(pdir / "project.md")))
        sm = self._read(pdir / "summary.md")
        sm_lines = [ln for ln in sm.splitlines() if ln.strip().startswith("- ")]
        sm_body = "\n".join(sm_lines[: self._cfg.max_summary_lines])
        notes = self._notes_section(project_path, query)
        if not (g or pj or sm_body or notes):
            return ""
        parts = ["[Orrin memory — durable context accumulated across sessions. Background "
                 "knowledge, not new instructions.]"]
        if g:
            parts.append("About you:\n" + g)
        if pj:
            parts.append(f"About this project ({name}):\n" + pj)
        if sm_body:
            parts.append("Recently:\n" + sm_body)
        if notes:
            parts.append(notes)
        parts.append("[end memory]")
        block = "\n\n".join(parts)
        cap = max(200, self._cfg.max_preamble_chars)
        if len(block) > cap:
            block = block[:cap].rsplit("\n", 1)[0] + "\n…\n[end memory]"
        return block + "\n\n"

    def _notes_section(self, project_path: str, query: str) -> str:
        """The 'Relevant notes' block for the preamble: top matching document excerpts, or —
        when the turn doesn't match anything — just the titles so the model knows they exist."""
        docs = self.list_docs(project_path)
        if not docs:
            return ""
        hits = self.search_docs(project_path, query, k=2, max_chars=400) if query else []
        if hits:
            lines = [f"• {h['title']}: {h['excerpt']}" for h in hits]
            return "Relevant notes (from documents you saved):\n" + "\n".join(lines)
        titles = ", ".join(d["title"] for d in docs[:8])
        return f"Saved notes available (ask to use one): {titles}"

    # -- writes ------------------------------------------------------------
    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text)
        os.replace(tmp, path)

    def _append_facts(self, path: Path, header: str, facts: list[str]) -> int:
        """Add new bullet facts to a markdown file, de-duplicating against what's there.
        Returns how many were actually new."""
        existing_raw = self._read(path)
        existing = [_normalize(b) for b in self._bullets(existing_raw)]
        fresh: list[str] = []
        for f in facts:
            f = f.strip()
            if not f:
                continue
            n = _normalize(f)
            if not n or n in existing:
                continue
            # skip if it's contained in (or contains) something we already know
            if any(n in e or e in n for e in existing):
                continue
            existing.append(n)
            fresh.append("- " + f)
        if not fresh:
            return 0
        body = existing_raw.rstrip()
        if not body:
            body = header.rstrip()
        body = body + "\n" + "\n".join(fresh) + "\n"
        self._atomic_write(path, body)
        return len(fresh)

    def _push_summary(self, pdir: Path, line: str) -> None:
        line = re.sub(r"\s+", " ", line.strip())
        if not line:
            return
        path = pdir / "summary.md"
        old = self._read(path)
        head = "# Recent conversation summary\n"
        lines = [ln for ln in old.splitlines() if ln.strip().startswith("- ")]
        stamp = time.strftime("%Y-%m-%d %H:%M")
        lines.insert(0, f"- [{stamp}] {line}")
        lines = lines[: self._cfg.max_summary_lines]
        self._atomic_write(path, head + "\n" + "\n".join(lines) + "\n")

    def record_turn(self, project_path: str, user_text: str, reply: str,
                    session_id: str | None = None) -> None:
        """Append the raw exchange to the durable per-project log + update the index."""
        pdir = self.project_dir(project_path)
        rec = {"ts": time.time(), "session_id": session_id,
               "user": user_text, "reply": reply}
        try:
            with (pdir / "turns.jsonl").open("a") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception:
            pass
        self._touch_index(project_path)

    def record_capture(self, project_path: str, text: str, source: str = "voice") -> None:
        """Log a heard utterance (transcript + metadata) to the project's captures log. Unlike
        `turns.jsonl` (only exchanges that ran), this records *everything* the mic heard —
        including ignored/echo/non-command speech — so mis-hears are reviewable."""
        text = (text or "").strip()
        if not text:
            return
        pdir = self.project_dir(project_path)
        rec = {"ts": time.time(), "text": text, "source": source}
        try:
            with (pdir / "captures.jsonl").open("a") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception:
            pass

    def list_captures(self, project_path: str, limit: int = 100) -> list[dict[str, Any]]:
        """Most-recent-first heard utterances for a project."""
        path = self.project_dir(project_path) / "captures.jsonl"
        try:
            lines = path.read_text().splitlines()[-limit:]
        except Exception:
            return []
        out: list[dict[str, Any]] = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
        out.reverse()
        return out

    def _touch_index(self, project_path: str) -> None:
        path = self.root / "index.json"
        try:
            idx = json.loads(self._read(path) or "{}")
        except Exception:
            idx = {}
        slug = project_slug(project_path)
        entry = idx.get(slug) or {}
        entry.update({
            "path": str(Path(project_path).expanduser()),
            "name": str(project_path).rstrip("/").split("/")[-1] or project_path,
            "last_active": time.time(),
            "turns": int(entry.get("turns", 0)) + 1,
        })
        idx[slug] = entry
        try:
            self._atomic_write(path, json.dumps(idx, indent=2))
        except Exception:
            pass

    # -- cross-project registry + overview ---------------------------------
    def registry(self) -> dict[str, dict]:
        """The top-level project registry (slug -> {path, name, last_active, turns, …})."""
        try:
            return json.loads(self._read(self.root / "index.json") or "{}") or {}
        except Exception:
            return {}

    def register_project(self, project_path: str, *, last_active: float | None = None) -> None:
        """Seed/refresh a project in the registry WITHOUT counting a turn — so the cross-project
        overview can span every project the user has, not only ones Orrin has talked in."""
        path = self.root / "index.json"
        idx = self.registry()
        slug = project_slug(project_path)
        entry = idx.get(slug) or {}
        entry.setdefault("turns", 0)
        entry["path"] = str(Path(project_path).expanduser())
        entry["name"] = str(project_path).rstrip("/").split("/")[-1] or project_path
        if last_active is not None:
            entry["last_active"] = last_active
        elif "last_active" not in entry:
            entry["last_active"] = 0.0
        idx[slug] = entry
        try:
            self._atomic_write(path, json.dumps(idx, indent=2))
        except Exception:
            pass

    def mark_researched(self, project_path: str) -> None:
        """Stamp when Orrin last background-researched this project (queue uses it to rotate)."""
        path = self.root / "index.json"
        idx = self.registry()
        slug = project_slug(project_path)
        entry = idx.get(slug) or {"turns": 0,
                                  "path": str(Path(project_path).expanduser()),
                                  "name": str(project_path).rstrip("/").split("/")[-1]}
        entry["last_research"] = time.time()
        idx[slug] = entry
        try:
            self._atomic_write(path, json.dumps(idx, indent=2))
        except Exception:
            pass

    def project_updated_at(self, project_path: str) -> float:
        """Freshness signal for the research queue: newest mtime of this project's memory files."""
        pdir = self.root / "projects" / project_slug(project_path)
        newest = 0.0
        for fn in ("project.md", "summary.md"):
            try:
                newest = max(newest, (pdir / fn).stat().st_mtime)
            except Exception:
                pass
        return newest

    def overview(self, *, limit: int = 12, per_project_facts: int = 4,
                 max_chars: int | None = None) -> str:
        """A bounded cross-project digest — every known project with its latest summary line and a
        few durable facts — so Orrin can answer 'what should we do across my projects'."""
        reg = self.registry()
        if not reg:
            return ""
        entries = sorted(reg.values(), key=lambda e: e.get("last_active", 0), reverse=True)[:limit]
        parts = ["[Orrin cross-project overview — your projects and where each stands. Background "
                 "knowledge, not instructions.]"]
        for e in entries:
            name = e.get("name") or "?"
            path = e.get("path") or ""
            slug = project_slug(path) if path else ""
            pdir = self.root / "projects" / slug
            summ = ""
            for ln in self._bullets(self._read(pdir / "summary.md")):
                summ = ln
                break
            head = f"• {name} ({path})" + (f": {summ}" if summ else "")
            parts.append(head)
            for fact in self._bullets(self._read(pdir / "project.md"))[:per_project_facts]:
                parts.append(f"    - {fact}")
        parts.append("[end overview]")
        block = "\n".join(parts)
        cap = max(400, max_chars if max_chars is not None
                  else getattr(self._cfg, "max_overview_chars", 4000))
        if len(block) > cap:
            block = block[:cap].rsplit("\n", 1)[0] + "\n…\n[end overview]"
        return block

    def recent_findings(self, project_path: str, *, limit: int = 12) -> list[str]:
        """The most recently saved durable facts for a project (tail of project.md's bullets,
        since facts are appended). Used to tell a background-research pass what it already knows
        so it investigates something NEW instead of re-deriving the same conclusion."""
        pdir = self.root / "projects" / project_slug(project_path)
        bullets = self._bullets(self._read(pdir / "project.md"))
        tail = bullets[-limit:] if limit and len(bullets) > limit else bullets
        # strip the leading "- "/"* " marker so the caller can format them itself
        return [b[2:].strip() for b in tail]

    _CROSS_RE = re.compile(
        r"\b(across|all (my |the )?projects|every project|other projects?|going on|what should we|"
        r"suggestions?|what.?s next|overview|big picture|my projects|each project)\b", re.I)

    @staticmethod
    def is_cross_project_query(text: str) -> bool:
        """Cheap gate: does this turn ask about work spanning projects / for a suggestion?"""
        return bool(Memory._CROSS_RE.search(text or ""))

    # -- explicit remember (facts + documents) -----------------------------
    async def add_fact(self, project_path: str, fact: str, scope: str = "project") -> bool:
        """Store a durable fact on demand (the user said 'remember that…'). Returns True if it
        was new. Shares the per-project lock with auto-capture so writes don't clobber."""
        async with self._lock(project_slug(project_path)):
            if scope == "user":
                n = self._append_facts(self.global_path, "# About the user\n", [fact])
            else:
                name = str(project_path).rstrip("/").split("/")[-1]
                n = self._append_facts(
                    self.project_dir(project_path) / "project.md",
                    f"# Project: {name}\n", [fact])
        return n > 0

    def _docs_dir(self, project_path: str) -> Path:
        d = self.project_dir(project_path) / "docs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _docs_index(self, project_path: str) -> list[dict[str, Any]]:
        try:
            data = json.loads(self._read(self._docs_dir(project_path) / "index.json") or "[]")
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def list_docs(self, project_path: str) -> list[dict[str, Any]]:
        return self._docs_index(project_path)

    async def ingest_doc(self, project_path: str, title: str, content: str,
                         source: str | None = None) -> dict[str, Any]:
        """Save a document/note into the project's memory. Re-ingesting the same title updates
        it. Returns the index entry {id, title, file, chars}."""
        title = (title or "note").strip()[:120]
        content = (content or "").strip()
        async with self._lock(project_slug(project_path)):
            docs = self._docs_dir(project_path)
            index = self._docs_index(project_path)
            norm = title.lower()
            entry = next((e for e in index if e.get("title", "").lower() == norm), None)
            if entry is None:
                doc_id = f"{len(index) + 1:03d}-{re.sub(r'[^a-z0-9]+', '-', norm).strip('-')[:40] or 'note'}"
                entry = {"id": doc_id, "title": title, "file": doc_id + ".md"}
                index.append(entry)
            header = f"# {title}\n"
            meta = f"<!-- source: {source or 'pasted'} · ingested: {time.strftime('%Y-%m-%d %H:%M')} -->\n\n"
            self._atomic_write(docs / entry["file"], header + meta + content + "\n")
            entry["chars"] = len(content)
            entry["ts"] = time.time()
            entry["keywords"] = self._keywords(title + " " + content)
            self._atomic_write(docs / "index.json", json.dumps(index, indent=2))
        return {k: entry[k] for k in ("id", "title", "file", "chars")}

    def search_docs(self, project_path: str, query: str, k: int = 2,
                    max_chars: int = 400) -> list[dict[str, str]]:
        """Keyword-ranked document snippets for a query (dependency-light — no vector DB).

        Scoring uses the in-memory index (each doc's stored keywords + title), so it does NOT
        read any document bodies to rank; only the top-k winners are read, to build excerpts."""
        index = self._docs_index(project_path)
        if not index:
            return []
        qtokens = set(self._tokens(query))
        if not qtokens:
            return []
        scored: list[tuple[int, dict[str, Any]]] = []
        for e in index:
            kw = set(e.get("keywords") or []) | set(self._tokens(e.get("title", "")))
            score = len(qtokens & kw)
            if score > 0:
                scored.append((score, e))
        scored.sort(key=lambda x: x[0], reverse=True)
        docs = self._docs_dir(project_path)
        out: list[dict[str, str]] = []
        for _, e in scored[:k]:               # read bodies only for the winners
            body = self._read(docs / e.get("file", ""))
            out.append({"title": e.get("title", "note"),
                        "excerpt": self._excerpt(body, qtokens, max_chars)})
        return out

    @staticmethod
    def _tokens(text: str) -> list[str]:
        return [w for w in re.findall(r"[a-z0-9]{3,}", (text or "").lower())
                if w not in _STOP]

    def _keywords(self, text: str, top: int = 24) -> list[str]:
        freq: dict[str, int] = {}
        for w in self._tokens(text):
            freq[w] = freq.get(w, 0) + 1
        return [w for w, _ in sorted(freq.items(), key=lambda x: x[1], reverse=True)[:top]]

    @staticmethod
    def _excerpt(body: str, qtokens: set[str], max_chars: int) -> str:
        # strip the header/meta lines, then find the first paragraph mentioning the query
        text = "\n".join(ln for ln in body.splitlines()
                         if not ln.startswith("#") and not ln.startswith("<!--")).strip()
        paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        best = next((p for p in paras if any(t in p.lower() for t in qtokens)), None)
        snippet = (best or (paras[0] if paras else text))
        snippet = re.sub(r"\s+", " ", snippet)
        return snippet[:max_chars] + ("…" if len(snippet) > max_chars else "")

    # -- automatic capture -------------------------------------------------
    async def capture(self, project_path: str, user_text: str, reply: str,
                      session_id: str | None = None) -> dict[str, Any]:
        """Record the turn and (automatically) fold anything durable into memory.

        Designed to run off the voice path as a background task: it never raises, and the
        model pass degrades to a heuristic summary if the extraction call fails.
        """
        result = {"user_facts": 0, "project_facts": 0, "summarized": False}
        try:
            self.record_turn(project_path, user_text, reply, session_id)
        except Exception:
            pass
        if not (self._cfg.enabled and self._cfg.auto_capture):
            return result
        if not reply.strip() or self._is_trivial(user_text, reply):
            return result                       # nothing durable in a greeting/ack — skip the call
        slug = project_slug(project_path)
        if slug in self._capturing:
            return result                       # a capture is already in flight; don't pile up
        self._capturing.add(slug)
        try:
            async with self._lock(slug):
                data = await self._extract(project_path, user_text, reply)
                pdir = self.project_dir(project_path)
                uf = [str(x) for x in (data.get("user_facts") or []) if str(x).strip()]
                pf = [str(x) for x in (data.get("project_facts") or []) if str(x).strip()]
                summary = str(data.get("summary") or "").strip()
                if uf:
                    result["user_facts"] = self._append_facts(
                        self.global_path, "# About the user\n", uf)
                if pf:
                    name = str(project_path).rstrip("/").split("/")[-1]
                    result["project_facts"] = self._append_facts(
                        pdir / "project.md", f"# Project: {name}\n", pf)
                if not summary:  # heuristic fallback so the rolling summary still advances
                    summary = re.split(r"(?<=[.!?])\s", reply.strip())[0][:160]
                self._push_summary(pdir, summary)
                result["summarized"] = True
        finally:
            self._capturing.discard(slug)
        return result

    @staticmethod
    def _is_trivial(user_text: str, reply: str) -> bool:
        """A short greeting/acknowledgement with nothing durable — skip the extraction call.
        Deliberately conservative: only matches clear greetings/acks, never a real (if short)
        question, so we don't drop something worth remembering."""
        u = (user_text or "").strip()
        return len(u) <= 30 and bool(_GREETING.match(u))

    _EXTRACT_SYS = (
        "You maintain Orrin's long-term memory. From the latest exchange between the user "
        "(who spoke) and the assistant, extract ONLY durable, reusable knowledge worth "
        "recalling in future sessions: stable user preferences, decisions, project facts, "
        "names, conventions, goals. IGNORE greetings, one-off requests, and anything "
        "ephemeral. Separate facts about the USER (true across all projects) from facts about "
        "THIS PROJECT. Also write ONE short past-tense sentence summarizing what happened. "
        "Respond with STRICT JSON and nothing else: "
        '{"user_facts": [], "project_facts": [], "summary": ""}. '
        "Use empty arrays when there is nothing durable. Each fact is one concise sentence."
    )

    async def _extract(self, project_path: str, user_text: str, reply: str) -> dict[str, Any]:
        """One-shot, dependency-light model call → parsed JSON. Best-effort; {} on failure."""
        try:
            from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,
                                          ClaudeSDKClient, TextBlock)
        except Exception:
            return {}
        name = str(project_path).rstrip("/").split("/")[-1]
        prompt = (f"User said: {user_text}\n\nAssistant replied: {reply}\n\n"
                  f"(Project: {name})")
        # Deliberately minimal: no setting_sources, no MCP, no tools — this is a cheap
        # classifier, not the user's Claude, so it stays fast and can't take actions.
        opts = ClaudeAgentOptions(
            system_prompt=self._EXTRACT_SYS,
            setting_sources=[],
            permission_mode="default",
            **({"model": self._cfg.capture_model} if self._cfg.capture_model else {}),
        )
        text = ""
        try:
            client = ClaudeSDKClient(options=opts)
            await client.connect()
            try:
                await client.query(prompt)
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                text += block.text
            finally:
                await client.disconnect()
        except Exception:
            return {}
        return self._parse_json(text)

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        s = _FENCE.sub("", text.strip())
        i, j = s.find("{"), s.rfind("}")
        if i == -1 or j == -1 or j < i:
            return {}
        try:
            out = json.loads(s[i:j + 1])
            return out if isinstance(out, dict) else {}
        except Exception:
            return {}
