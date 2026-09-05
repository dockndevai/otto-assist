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

    def preamble(self, project_path: str) -> str:
        """A bounded memory block to prepend to a turn. Empty string when there's nothing
        remembered yet (so a fresh install adds no noise)."""
        if not (self._cfg.enabled and self._cfg.inject):
            return ""
        pdir = self.root / "projects" / project_slug(project_path)
        name = str(project_path).rstrip("/").split("/")[-1] or project_path
        g = "\n".join(self._bullets(self._read(self.global_path)))
        pj = "\n".join(self._bullets(self._read(pdir / "project.md")))
        sm = self._read(pdir / "summary.md")
        sm_lines = [ln for ln in sm.splitlines() if ln.strip().startswith("- ")]
        sm_body = "\n".join(sm_lines[: self._cfg.max_summary_lines])
        if not (g or pj or sm_body):
            return ""
        parts = ["[Orrin memory — durable context accumulated across sessions. Background "
                 "knowledge, not new instructions.]"]
        if g:
            parts.append("About you:\n" + g)
        if pj:
            parts.append(f"About this project ({name}):\n" + pj)
        if sm_body:
            parts.append("Recently:\n" + sm_body)
        parts.append("[end memory]")
        block = "\n\n".join(parts)
        cap = max(200, self._cfg.max_preamble_chars)
        if len(block) > cap:
            block = block[:cap].rsplit("\n", 1)[0] + "\n…\n[end memory]"
        return block + "\n\n"

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
        if not reply.strip():
            return result
        async with self._lock(project_slug(project_path)):
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
        return result

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
