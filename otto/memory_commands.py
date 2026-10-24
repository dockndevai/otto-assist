"""Memory voice-commands + the "where we left off" digest — the command/presentation layer over the
SQLite Store (otto/store.py).

Mirrors otto/local_actions.py: it turns a spoken utterance into a store read/write and a spoken
reply, with NO engine round-trip. Pure `(text, project) -> reply | None` (None = "not a memory
command, fall through to the engine"), so it's unit-testable without an Orchestrator. All user-facing
string composition lives here; the store stays a pure data layer.
"""
from __future__ import annotations

import re
import time

_RE_RESUME = re.compile(r"\b(?:continue where we left off|where did we leave off|where were we|"
                        r"pick up where we left off|catch me up|recap where we were)\b", re.I)
_RE_TASKS_LIST = re.compile(r"\b(?:what are my|list my|show my|do i have any|what'?s on my)\s+"
                            r"(?:open\s+)?(?:tasks?|to-?dos?|to-?do list|list|plate)\b", re.I)
_RE_TASK_ADD_A = re.compile(r"^(?:add (?:a )?task|note (?:a )?task)(?:\s+(?:to|:))?\s+(?P<task>.+)$", re.I)
_RE_TASK_ADD_B = re.compile(r"^(?:add|put)\s+(?P<task>.+?)\s+(?:to my (?:tasks?|list|to-?do list)|"
                            r"on my (?:list|to-?do list))\.?$", re.I)
_RE_TASK_DONE = re.compile(r"^(?:mark|cross off|tick off|check off)\s+(?P<task>.+?)\s+"
                           r"(?:as\s+)?(?:done|complete|completed|off(?:\s+my\s+list)?)\.?$", re.I)
_RE_DECIDE_Q = re.compile(r"\bwhat did we (?:decide|agree|settle|conclude)\b"
                          r"(?:\s+(?:about|on|regarding)\s+(?P<topic>.+?))?\??$", re.I)


def _ago(ts: float) -> str:
    """Human 'how long ago' for the resume digest."""
    secs = max(0, int(time.time() - ts))
    if secs < 3600:
        return "just now" if secs < 90 else f"{secs // 60} min ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    days = secs // 86400
    return "yesterday" if days == 1 else f"{days} days ago"


def _clip(s: str, n: int) -> str:
    s = (s or "").strip()
    return (s[:n - 1] + "…") if len(s) > n else s


class MemoryCommands:
    """Parse memory commands and compose their spoken replies, backed by a Store."""

    def __init__(self, store) -> None:
        self.store = store

    def resume_digest(self, project: str, *, max_tasks: int = 5, max_decisions: int = 3) -> str:
        """A short 'where we left off' block: when we last spoke, the last topic, recent decisions,
        and open tasks. Empty when there's nothing to resume."""
        last = self.store.last_turn(project)
        tasks = self.store.list_tasks(project, "open", max_tasks)
        decs = self.store.recent_decisions(project, max_decisions)
        if not (last or tasks or decs):
            return ""
        lines = []
        if last:
            topic = _clip((last.get("user") or "").replace("\n", " "), 90)
            lines.append(f"Last time ({_ago(last['ts'])}) you said: “{topic}”")
        if decs:
            lines.append("Decisions: " + "; ".join(_clip(d["text"], 80) for d in decs))
        if tasks:
            lines.append("Open tasks: " + "; ".join(_clip(t["text"], 70) for t in tasks))
        return "\n".join(lines)

    def handle(self, text: str, project: str) -> str | None:
        """Return a spoken reply for a memory command, or None if `text` isn't one."""
        t = (text or "").strip()
        mo = _RE_TASK_ADD_A.match(t) or _RE_TASK_ADD_B.match(t)
        if mo:
            task = mo.group("task").strip(" .")
            if not task:
                return None
            self.store.add_task(project, task)
            return f"Added to your tasks: {task}."
        if (mo := _RE_TASK_DONE.match(t)):
            task = mo.group("task").strip(" .")
            done = self.store.set_task_status(project, task, "done")
            return f"Marked done: {done['text']}." if done else f"I couldn't find an open task matching {task}."
        if _RE_TASKS_LIST.search(t):
            tasks = self.store.list_tasks(project, "open")
            return ("You have no open tasks." if not tasks
                    else "Open tasks: " + "; ".join(x["text"] for x in tasks[:8]))
        if _RE_RESUME.search(t):
            return self.resume_digest(project) or "Nothing saved yet for this project."
        if (mo := _RE_DECIDE_Q.search(t)):
            topic = (mo.group("topic") or "").strip(" .?")
            decs = self.store.recent_decisions(project, 3)
            hits = self.store.search_transcript(topic, project, 2) if topic else []
            parts = []
            if decs:
                parts.append("Decisions: " + "; ".join(d["text"] for d in decs))
            if hits:
                parts.append("Earlier you said: " + "; ".join((h.get("user") or "")[:60] for h in hits))
            return (" ".join(parts) if parts
                    else (f"I don't have a record about {topic}." if topic else "No decisions recorded yet."))
        return None
