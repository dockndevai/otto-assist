"""Generic CLI-agent adapter — drive ANY headless command-line coding agent (Cursor, Aider, Gemini
CLI, Codex, opencode, a custom one) as an Orrin `Brain`, so engines are interchangeable and multiple
can run at once (one subprocess per Workspace).

It satisfies the neutral Brain contract (otto/brain/base.py): `connect / disconnect / interrupt /
send(text) -> AsyncIterator[dict]` + a `session_id` attr, yielding exactly the same event dicts the
SDK ClaudeBrain does — `{"type":"delta","text"}`, `{"type":"tool_use","name","detail"}`,
`{"type":"result","is_error","cost","error_text","session_id"}`. Everything tool-specific lives in an
`AgentProfile` (config data), so a new tool needs no code. Subprocesses are spawned argv-only (no
shell → no injection). Safety: autonomy/auto-approve args are added only under write-mode or
permission=bypass; by default the CLI runs in its own safe mode and Orrin's task-level confirm gates
destructive requests (a subprocess agent's per-tool calls can't be intercepted like the SDK path).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import uuid
from typing import Any, AsyncIterator

_AUTH_ERR = re.compile(r"\b(not logged in|unauthorized|authentication|auth error|please log ?in|"
                       r"rate.?limit|quota|forbidden|401|403|429|no api key|invalid api key)\b", re.I)


def _as_str(v: Any) -> str:
    return v if isinstance(v, str) else ""


def _dig_text(obj: Any) -> str:
    """Best-effort pull of assistant text from one stream-json object across common CLI schemas."""
    if not isinstance(obj, dict):
        return ""
    for key in ("text",):
        if _as_str(obj.get(key)):
            return obj[key]
    delta = obj.get("delta")
    if isinstance(delta, dict) and _as_str(delta.get("text")):
        return delta["text"]
    for container in (obj.get("content"), (obj.get("message") or {}).get("content")
                      if isinstance(obj.get("message"), dict) else None):
        if isinstance(container, str) and container:
            return container
        if isinstance(container, list):
            parts = [c["text"] for c in container
                     if isinstance(c, dict) and _as_str(c.get("text"))]
            if parts:
                return "".join(parts)
    return ""


def _dig_tool(obj: Any) -> tuple[str, str] | None:
    """Best-effort pull of a tool invocation from one stream-json object."""
    if not isinstance(obj, dict):
        return None
    typ = _as_str(obj.get("type")).lower()
    name = _as_str(obj.get("tool_name")) or _as_str(obj.get("tool")) or \
        (_as_str(obj.get("name")) if "tool" in typ else "")
    if not name:
        return None
    detail = ""
    for k in ("command", "cmd", "detail", "file", "path", "query"):
        if _as_str(obj.get(k)):
            detail = obj[k]
            break
    if not detail:
        inp = obj.get("input") or obj.get("args")
        if isinstance(inp, dict):
            for k in ("command", "cmd", "file_path", "path", "query", "pattern"):
                if _as_str(inp.get(k)):
                    detail = inp[k]
                    break
    return name, (detail[:80] if detail else "")


class CliAgentBrain:
    """A `Brain` backed by a headless CLI agent, configured by an `AgentProfile`."""

    def __init__(self, ctx: Any, settings: Any, profile: Any, *, resume: str | None = None,
                 new_chat: bool = False, cwd: str | None = None, read_only: bool = False,
                 write_mode: bool = False) -> None:
        self.ctx = ctx
        self.settings = settings
        self.profile = profile
        self.cwd = cwd or os.path.expanduser("~")
        self.read_only = read_only
        self.write_mode = write_mode
        self.session_id: str | None = None if new_chat else resume
        self._exe: str | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._unavailable = ""

    # ---- Brain protocol ------------------------------------------------------
    async def connect(self) -> None:
        self._exe = self._resolve_command()
        self._unavailable = "" if self._exe else \
            f"'{self.profile.command}' not found on PATH"

    async def disconnect(self) -> None:
        await self._kill()

    async def reconnect(self) -> None:
        await self.connect()

    async def interrupt(self) -> None:
        await self._kill()

    async def send(self, text: str) -> AsyncIterator[dict[str, Any]]:
        if self._exe is None and not self._unavailable:
            await self.connect()
        if self._unavailable:
            yield self._error(f"{self._label()} unavailable — {self._unavailable}. "
                              f"Is it installed and on your PATH?")
            return
        argv = self._build_argv(text)
        stdin_data = text.encode() if self.profile.prompt_via == "stdin" else None
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv, cwd=self.cwd, env={**os.environ, **(self.profile.env or {})},
                stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except Exception as e:
            yield self._error(f"couldn't start {self._label()}: {e}")
            return
        proc = self._proc
        if stdin_data is not None and proc.stdin is not None:
            try:
                proc.stdin.write(stdin_data)
                await proc.stdin.drain()
                proc.stdin.close()
            except Exception:
                pass

        emitted = False
        raw: list[str] = []
        assert proc.stdout is not None
        async for line in proc.stdout:
            if self.profile.output_format == "stream-json":
                for ev in self._parse_stream_json(line):
                    if ev.get("type") == "delta" and ev.get("text"):
                        emitted = True
                    yield ev
            else:
                s = line.decode(errors="replace")
                raw.append(s)
                if s.strip():
                    emitted = True
                    yield {"type": "delta", "text": s}

        rc = await proc.wait()
        err = ""
        if proc.stderr is not None:
            try:
                err = (await proc.stderr.read()).decode(errors="replace").strip()
            except Exception:
                err = ""
        self._proc = None
        is_error = rc != 0 or bool(_AUTH_ERR.search(err))
        # Safety net: if we parsed nothing but the run succeeded, speak the raw stdout.
        if not emitted and not is_error:
            body = "".join(raw).strip()
            if body:
                yield {"type": "delta", "text": body}
        yield {"type": "result", "is_error": is_error, "cost": 0.0,
               "error_text": (err or f"{self._label()} exited with code {rc}") if is_error else "",
               "session_id": self.session_id}

    # ---- internals -----------------------------------------------------------
    def _label(self) -> str:
        return self.profile.label or self.profile.command or "agent"

    def _error(self, msg: str) -> dict[str, Any]:
        return {"type": "result", "is_error": True, "cost": 0.0, "error_text": msg,
                "session_id": self.session_id}

    def _resolve_command(self) -> str | None:
        for c in str(self.profile.command or "").split(","):
            c = c.strip()
            if c and shutil.which(c):
                return shutil.which(c)
        return None

    async def _kill(self) -> None:
        p, self._proc = self._proc, None
        if p is not None and p.returncode is None:
            try:
                p.terminate()
                await asyncio.wait_for(p.wait(), timeout=2.0)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    def _fill(self, tokens: list[str], prompt: str) -> list[str]:
        sub = {"{prompt}": prompt, "{cwd}": self.cwd, "{session}": self.session_id or ""}
        out = []
        for t in tokens:
            if t == "{prompt}" and self.profile.prompt_via == "stdin":
                continue
            for k, v in sub.items():
                t = t.replace(k, v)
            out.append(t)
        return out

    def _build_argv(self, prompt: str) -> list[str]:
        if self.profile.session_from == "generate" and not self.session_id:
            self.session_id = uuid.uuid4().hex
        argv = [self._exe] + self._fill(list(self.profile.args), prompt)
        if self.session_id and self.profile.resume_args:
            argv += self._fill(list(self.profile.resume_args), prompt)
        if (self.profile.session_from == "generate" and self.session_id
                and self.profile.session_arg):
            argv += self._fill(list(self.profile.session_arg), prompt)
        allow_autonomy = self.write_mode or getattr(self.settings.brain, "permission", "") == "bypass"
        if allow_autonomy and self.profile.autonomy_args:
            argv += self._fill(list(self.profile.autonomy_args), prompt)
        return argv

    def _parse_stream_json(self, line: bytes) -> list[dict[str, Any]]:
        s = line.decode(errors="replace").strip()
        if not s:
            return []
        try:
            obj = json.loads(s)
        except Exception:
            return [{"type": "delta", "text": s + "\n"}]   # plain (non-JSON) line
        if self.profile.session_from in ("auto", "generate") and not self.session_id:
            for k in ("session_id", "sessionId", "chat_id", "chatId", "conversation_id", "id"):
                if isinstance(obj, dict) and _as_str(obj.get(k)):
                    self.session_id = obj[k]
                    break
        out: list[dict[str, Any]] = []
        txt = _dig_text(obj)
        if txt:
            out.append({"type": "delta", "text": txt})
        tool = _dig_tool(obj)
        if tool:
            out.append({"type": "tool_use", "name": tool[0], "detail": tool[1]})
        return out
