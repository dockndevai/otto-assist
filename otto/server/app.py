"""FastAPI app: dashboard + settings UI, a WebSocket event stream, a small config/projects
API, and (optionally) the Otto MCP server mounted at /mcp for Claude to connect to."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import yaml
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import config_path, save_config
from ..orchestrator import Orchestrator
from ..projects import list_projects
from ..sessions import list_sessions
from .events import EventBus

WEB_DIR = Path(__file__).parent / "web"
_log = logging.getLogger("otto.server")


def _redact_secrets(obj):
    """Deep-copy a config dict/list with any secret-looking values blanked, so the dashboard's
    GET /api/config can never hand out server.auth_token or stray API keys (#46)."""
    import re as _re
    secret = _re.compile(r"(token|secret|password|passwd|api[_-]?key|apikey|bearer|credential)",
                         _re.I)
    def walk(o):
        if isinstance(o, dict):
            return {k: ("" if isinstance(v, str) and v and secret.search(str(k)) else walk(v))
                    for k, v in o.items()}
        if isinstance(o, list):
            return [walk(v) for v in o]
        return o
    return walk(obj)


def _restore_blank_secrets(new, old):
    """In-place: wherever `new` has a blank secret-looking value but `old` had a real one, copy
    the old value back. Lets GET redact secrets without a subsequent round-trip PUT erasing them."""
    import re as _re
    secret = _re.compile(r"(token|secret|password|passwd|api[_-]?key|apikey|bearer|credential)",
                         _re.I)
    if not isinstance(new, dict) or not isinstance(old, dict):
        return
    for k, v in list(new.items()):
        if isinstance(v, dict):
            _restore_blank_secrets(v, old.get(k) if isinstance(old.get(k), dict) else {})
        elif secret.search(str(k)) and (v == "" or v is None):
            ov = old.get(k)
            if isinstance(ov, str) and ov:
                new[k] = ov


def _local_addresses() -> list[str]:
    """Best-effort list of IPv4 addresses the phone could reach this Mac on — the LAN address
    and any Tailscale address (100.64/10, CGNAT range). Ordered Tailscale-first since that's
    the one that works from anywhere. Never raises."""
    import socket

    addrs: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in addrs and not ip.startswith("127."):
                addrs.append(ip)
    except Exception:
        pass
    # A UDP connect() reveals the primary outbound LAN IP without sending anything.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip not in addrs:
            addrs.append(ip)
    except Exception:
        pass
    # Tailscale addresses (100.64.0.0/10) work from anywhere — surface them first.
    tailscale = [a for a in addrs if a.startswith("100.")]
    others = [a for a in addrs if not a.startswith("100.")]
    return tailscale + others


def create_app(orch: Orchestrator, bus: EventBus) -> FastAPI:
    # Stream warnings/errors (and Otto's diagnostics) to the dashboard Logs drawer, and keep
    # a ring buffer to replay to newly-connected dashboards.
    from ..logbus import install_log_bus
    log_buffer = install_log_bus(bus)

    # Build the MCP sub-app first so we can share its lifespan with FastAPI.
    mcp_app = None
    lifespan = None
    if orch.settings.brain.register_mcp:
        try:
            from ..mcpserver import build_mcp

            mcp_app = build_mcp(orch).http_app(path="/", stateless_http=True)
            lifespan = mcp_app.lifespan
        except Exception:
            mcp_app = None

    app = FastAPI(title="Orrin", lifespan=lifespan)

    # -- access guard ------------------------------------------------------
    # The server binds to loopback, but browsers do NOT apply CORS to WebSockets and will send
    # cross-site POSTs, so a random web page could otherwise drive the agent (submit turns,
    # switch project, change config). Reject any request whose Origin isn't our own dashboard,
    # and require the Host header to be loopback (defeats DNS-rebinding). Requests with no Origin
    # (local CLI tools, the MCP client) are allowed — they can't be forged by a remote page.
    #
    # Remote access (the mobile app) opts in via server.allow_remote + a bearer token: an
    # authenticated request is trusted regardless of Origin/Host, because a random web page can't
    # know the token. Everything else still falls back to the loopback-only rule, so with remote
    # OFF the behaviour is exactly as before.
    import hmac
    import secrets as _secrets

    _port = orch.settings.server.port
    _ok_origins = {f"http://127.0.0.1:{_port}", f"http://localhost:{_port}"}
    _ok_hosts = {f"127.0.0.1:{_port}", f"localhost:{_port}", "127.0.0.1", "localhost"}
    _LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}

    _srv = orch.settings.server
    # #52: full access + remote is remote code execution for anyone who has (or forges their way
    # past) the token. Make the combination loud rather than silent.
    if _srv.allow_remote and orch.settings.brain.permission in ("bypass", "auto"):
        _log.warning("SECURITY: remote access is ON together with permission=%s (full access). "
                     "A remote client with the token can run anything as you. Consider "
                     "'guarded' or 'voice', and rotate server.auth_token if it may have leaked.",
                     orch.settings.brain.permission)
        # destructive_guard is the only confirm left standing in this combination (see its
        # docstring in config.py) — never let a config override silently drop it and leave a
        # remote token-holder with zero confirmation on destructive commands.
        if not orch.settings.voice.destructive_guard:
            _log.warning("SECURITY: voice.destructive_guard was off — forcing it on, since it's "
                         "the only confirm gate left while remote+%s is active.",
                         orch.settings.brain.permission)
            orch.settings.voice.destructive_guard = True
    if _srv.allow_remote and not _srv.auth_token:
        # First time remote is enabled with no token: mint a strong one and persist it, so the
        # user can read it (menu bar / GET /api/pairing) and paste it into the phone app — and
        # so it stays STABLE across restarts (otherwise the phone would need re-pairing every
        # launch). save_config takes the full config *dict*, not the settings object.
        _srv.auth_token = _secrets.token_urlsafe(24)
        try:
            cfg = config_path()
            data = (yaml.safe_load(cfg.read_text()) if cfg.exists() else {}) or {}
            data.setdefault("server", {})
            data["server"]["allow_remote"] = True
            data["server"]["auth_token"] = _srv.auth_token
            save_config(data)
        except Exception:
            pass

    def _authed(headers, query_token: str | None = None) -> bool:
        """True if remote access is on and the request presents the correct bearer token,
        via `Authorization: Bearer <token>` or a `?token=` query param (WS fallback)."""
        token = orch.settings.server.auth_token
        if not (orch.settings.server.allow_remote and token):
            return False
        presented = query_token
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            presented = auth[7:].strip()
        return bool(presented) and hmac.compare_digest(presented, token)

    def _peer_loopback(client) -> bool:
        return bool(client) and getattr(client, "host", None) in _LOOPBACK

    def _peer_local(scope) -> bool:
        """A trusted LOCAL caller: the socket peer is actually loopback AND the Host header is
        loopback. Both are required (#46): the Host header alone is forgeable by any LAN/Tailnet
        client, and a DNS-rebound page reaches a loopback peer but carries a foreign Host — this
        catches both. `scope` is a Request or WebSocket (both expose .client and .headers)."""
        return _peer_loopback(scope.client) and scope.headers.get("host", "") in _ok_hosts

    def _origin_ok(headers) -> bool:
        origin = headers.get("origin")
        return origin is None or origin in _ok_origins

    def _trusted(scope, query_token=None) -> bool:
        """Either an authenticated remote client (bearer token — unforgeable by a web page), or a
        genuine loopback caller whose Origin is our own dashboard (CSRF defence for browsers)."""
        if _authed(scope.headers, query_token):
            return True
        return _peer_local(scope) and _origin_ok(scope.headers)

    def _is_public_shell(path: str) -> bool:
        # The static UI shell carries no secrets and must load to bootstrap the dashboard (incl.
        # the phone's WKWebView), so it's the one thing served without a token. Every data
        # endpoint and every mutation goes through _trusted below.
        return path in ("/", "/settings", "/favicon.ico") or path.startswith("/static")

    @app.middleware("http")
    async def _guard(request, call_next):
        # Guard EVERYTHING that isn't the public UI shell — data GETs included, so history /
        # captures / memory / sessions / config can't be read via DNS-rebinding or by an
        # unauthenticated remote peer (#46). OPTIONS (CORS preflight) passes.
        if request.method != "OPTIONS" and not _is_public_shell(request.url.path):
            if not _trusted(request, request.query_params.get("token")):
                return JSONResponse({"error": "forbidden"}, status_code=403)
        resp = await call_next(request)
        # Never let the in-app WKWebView serve a stale dashboard/asset after an update — each
        # rebuild changes app.js/styles.css at the same URL, so cache them 'no-store'.
        p = request.url.path
        if p == "/" or p == "/settings" or p.startswith("/static"):
            resp.headers["Cache-Control"] = "no-store, must-revalidate"
        return resp

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/favicon.ico")
    async def favicon() -> FileResponse:
        return FileResponse(WEB_DIR / "favicon-32.png")

    @app.get("/settings")
    async def settings_page() -> FileResponse:
        return FileResponse(WEB_DIR / "settings.html")

    @app.get("/hud")
    async def hud_page() -> FileResponse:
        return FileResponse(WEB_DIR / "hud.html")

    @app.get("/enroll")
    async def enroll_page() -> FileResponse:
        return FileResponse(WEB_DIR / "enroll.html")

    # -- mobile pairing ---------------------------------------------------
    # What the phone needs to connect: the token + reachable addresses. Only served to a
    # LOOPBACK caller (the local dashboard / menu bar) so a remote client can't fetch the
    # secret it doesn't already have. Meant to back a "Pair phone" QR/details screen.
    @app.get("/api/pairing")
    async def get_pairing(request: Request) -> JSONResponse:
        if not _peer_local(request):
            return JSONResponse({"error": "pairing details are only available locally"},
                                status_code=403)
        srv = orch.settings.server
        return JSONResponse({
            "allow_remote": srv.allow_remote,
            "port": srv.port,
            "token": srv.auth_token,
            "addresses": _local_addresses(),   # LAN + Tailscale IPs to try
            "hint": ("Enable server.allow_remote and restart to accept phone connections."
                     if not srv.allow_remote else
                     "Paste an address + this token into the Orrin phone app."),
        })

    @app.get("/api/enrolled_voices")
    async def get_enrolled_voices(request: Request) -> JSONResponse:
        """Enrolled speaker voiceprint names, for the enrollment page's list. Loopback-only.
        (Distinct from /api/voices, which lists TTS output voices.)"""
        if not _peer_local(request):
            return JSONResponse({"error": "local only"}, status_code=403)
        return JSONResponse({"voices": orch.list_voices()})

    # -- config + projects API --------------------------------------------
    @app.get("/api/config")
    async def get_config() -> JSONResponse:
        cfg = config_path()
        data = yaml.safe_load(cfg.read_text()) if cfg.exists() else {}
        # Never hand out secrets (server.auth_token, stray API keys) even to a local caller — the
        # dashboard doesn't need them, and this file is the one place the token is persisted (#46).
        return JSONResponse({"config": _redact_secrets(data or {}), "path": str(cfg)})

    @app.put("/api/config")
    async def put_config(payload: dict) -> JSONResponse:
        data = payload.get("config", payload)
        # GET redacts secrets to "", and the settings UI round-trips the whole config, so a naive
        # save would erase server.auth_token. Preserve any secret that arrives blank from the
        # value already on disk (#46), so redaction can't nuke the phone-pairing token.
        try:
            cfg = config_path()
            old = (yaml.safe_load(cfg.read_text()) if cfg.exists() else {}) or {}
            _restore_blank_secrets(data, old)
        except Exception:
            pass
        save_config(data)
        return JSONResponse({"ok": True, "path": str(config_path()),
                             "note": "Some changes apply after Restart."})

    @app.get("/api/projects")
    async def get_projects() -> JSONResponse:
        projs = list_projects(current=orch.current_project,
                              pinned=orch.settings.brain.projects)
        return JSONResponse({"projects": [p.__dict__ for p in projs]})

    @app.get("/api/history")
    async def get_history(project: str = "") -> JSONResponse:
        """The stored dashboard conversation for a project (defaults to focused). Used by the
        front-end to fill a window when switching to a project not yet loaded this session."""
        events = orch.history.load(project or orch.current_project)
        return JSONResponse({"events": events})

    @app.post("/api/project/switch")
    async def switch_project(payload: dict) -> JSONResponse:
        path = payload.get("path", "")
        if not path:
            return JSONResponse({"ok": False, "error": "path required"}, status_code=400)
        asyncio.create_task(orch.switch_project(path))
        return JSONResponse({"ok": True})

    @app.post("/api/say")
    async def api_say(payload: dict) -> JSONResponse:
        """Send Otto a line of text and (by default) wait for its spoken reply — so an
        external voice front-end (a macOS/iOS Siri Shortcut that dictates the text) can drive
        Otto without relying on Otto's own microphone. Otto also speaks the reply aloud on the
        Mac as usual. Local (loopback) callers are allowed by the same guard as the dashboard;
        remote callers need the bearer token."""
        # Coerce defensively: a client sending text as a non-string (e.g. {"text": 123}) must get
        # a clean 400, not a 500 from calling .strip() on a non-str.
        raw = payload.get("text")
        if raw is not None and not isinstance(raw, str):
            return JSONResponse({"ok": False, "error": "text must be a string"}, status_code=400)
        text = (raw or "").strip()
        if not text:
            return JSONResponse({"ok": False, "error": "text required"}, status_code=400)
        if not payload.get("wait", True):
            orch.submit_user_input(text)
            return JSONResponse({"ok": True})
        # Subscribe BEFORE submitting so we don't miss the reply, then wait for the next
        # assistant-final transcript (Otto's answer to this turn).
        queue = bus.subscribe()
        try:
            orch.submit_user_input(text)
            loop = asyncio.get_event_loop()
            deadline = loop.time() + float(payload.get("timeout", 90))
            reply = ""
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                if (ev.get("type") == "transcript_final"
                        and ev.get("role") == "assistant"):
                    reply = ev.get("text") or ""
                    break
            return JSONResponse({"ok": True, "reply": reply})
        finally:
            bus.unsubscribe(queue)

    # -- parallel workspaces (each a project running its own Claude) -------
    @app.get("/api/workspaces")
    async def get_workspaces() -> JSONResponse:
        items = [{"project": ws.project, "name": ws.name, "state": ws.state,
                  "focused": ws.project == orch.focused, "session_id": ws.session_id}
                 for ws in orch.workspaces.values()]
        return JSONResponse({"workspaces": items, "focused": orch.focused,
                             "max": orch.max_workspaces})

    @app.post("/api/workspace/open")
    async def open_workspace(payload: dict) -> JSONResponse:
        path = payload.get("path", "")
        if not path:
            return JSONResponse({"ok": False, "error": "path required"}, status_code=400)
        asyncio.create_task(orch.open_workspace(path, focus=payload.get("focus", True)))
        return JSONResponse({"ok": True})

    @app.post("/api/workspace/focus")
    async def focus_workspace(payload: dict) -> JSONResponse:
        path = payload.get("path", "")
        if not path:
            return JSONResponse({"ok": False, "error": "path required"}, status_code=400)
        orch.focus_workspace(path)
        return JSONResponse({"ok": True})

    @app.post("/api/workspace/close")
    async def close_workspace(payload: dict) -> JSONResponse:
        path = payload.get("path", "")
        if not path:
            return JSONResponse({"ok": False, "error": "path required"}, status_code=400)
        asyncio.create_task(orch.close_workspace(path))
        return JSONResponse({"ok": True})

    @app.post("/api/workspace/clone")
    async def clone_workspace(payload: dict) -> JSONResponse:
        url = payload.get("url", "")
        if not url:
            return JSONResponse({"ok": False, "error": "url required"}, status_code=400)
        asyncio.create_task(orch.clone_workspace(url, payload.get("branch")))
        return JSONResponse({"ok": True})

    # -- chats (Claude sessions within the current project) ----------------
    @app.get("/api/sessions")
    async def get_sessions() -> JSONResponse:
        current = getattr(orch.brain, "session_id", None) or orch._active_session
        chats = list_sessions(orch.current_project, current_id=current)
        return JSONResponse({"sessions": [s.__dict__ for s in chats]})

    @app.post("/api/session/switch")
    async def switch_session(payload: dict) -> JSONResponse:
        sid = payload.get("session_id", "")
        if not sid:
            return JSONResponse({"ok": False, "error": "session_id required"}, status_code=400)
        asyncio.create_task(orch.switch_session(sid))
        return JSONResponse({"ok": True})

    @app.get("/api/all-sessions")
    async def get_all_sessions(q: str = "") -> JSONResponse:
        from ..sessions import list_all_sessions
        current = getattr(orch.brain, "session_id", None) or orch._active_session
        return JSONResponse({"sessions": list_all_sessions(query=q, current_id=current)})

    @app.post("/api/session/open-anywhere")
    async def open_session_anywhere(payload: dict) -> JSONResponse:
        project = payload.get("project", "")
        sid = payload.get("session_id", "")
        if not project or not sid:
            return JSONResponse({"ok": False, "error": "project and session_id required"},
                                status_code=400)
        asyncio.create_task(orch.open_session_anywhere(project, sid))
        return JSONResponse({"ok": True})

    @app.post("/api/session/new")
    async def new_session() -> JSONResponse:
        asyncio.create_task(orch.new_chat())
        return JSONResponse({"ok": True})

    @app.post("/api/session/rename")
    async def rename_session(payload: dict) -> JSONResponse:
        title = payload.get("title", "")
        if not title:
            return JSONResponse({"ok": False, "error": "title required"}, status_code=400)
        asyncio.create_task(orch.rename_chat(title))
        return JSONResponse({"ok": True})

    # -- live dashboard data (weather / github / calendar / mcp) ----------
    @app.get("/api/weather")
    def get_weather() -> JSONResponse:            # sync -> runs in a threadpool
        from . import data
        return JSONResponse({"weather": data.weather()})

    @app.get("/api/github")
    def get_github() -> JSONResponse:
        from . import data
        return JSONResponse({"github": data.github_activity(orch.focused)})

    @app.get("/api/system")
    def get_system() -> JSONResponse:
        from . import data
        b = orch.settings.brain
        return JSONResponse({"mcp_count": data.mcp_count(orch.focused),
                             "register_mcp": b.register_mcp})

    # -- voices (pick Otto's spoken voice) --------------------------------
    @app.get("/api/voices")
    async def get_voices() -> JSONResponse:
        from ..voice.tts import list_kokoro_voices, list_system_voices
        tts = orch.settings.voice.tts
        if tts.engine == "kokoro":
            voices, current = list_kokoro_voices(), tts.kokoro_voice
        else:
            voices, current = list_system_voices(), tts.voice
        return JSONResponse({"voices": voices, "current": current, "engine": tts.engine})

    @app.post("/api/voice/select")
    async def select_voice(payload: dict) -> JSONResponse:
        orch.set_voice(payload.get("voice", ""))
        return JSONResponse({"ok": True})

    @app.post("/api/voice/preview")
    async def preview_voice(payload: dict) -> JSONResponse:
        orch.preview_voice(payload.get("voice", ""))
        return JSONResponse({"ok": True})

    # -- captures (every heard utterance, per project) --------------------
    @app.get("/api/captures")
    async def get_captures(project: str = "", limit: int = 100) -> JSONResponse:
        items = orch.memory.list_captures(project or orch.current_project, limit=limit)
        return JSONResponse({"captures": items})

    # -- project memory (what Orrin remembers about the current project) ---
    @app.get("/api/memory")
    async def get_memory() -> JSONResponse:
        p = orch.current_project
        facts: list[str] = []
        summary: list[str] = []
        docs: list[str] = []
        try:
            pdir = orch.memory.project_dir(p)

            def _bullets(path):
                if not path.exists():
                    return []
                out = []
                for ln in path.read_text().splitlines():
                    s = ln.strip()
                    if s.startswith("- ") or s.startswith("* "):
                        out.append(s[2:].strip())
                return out
            facts = _bullets(pdir / "project.md")
            summary = _bullets(pdir / "summary.md")
            docs = [(d.get("title") or d.get("name") or "note")
                    for d in orch.memory.list_docs(p)][:8]
        except Exception:
            pass
        return JSONResponse({"project": p, "name": Path(p).name,
                             "facts": facts, "summary": summary,
                             "docs": docs, "doc_count": len(docs)})

    @app.get("/api/roadmap")
    async def get_roadmap() -> JSONResponse:
        """The latest cross-project prioritized plan from the daily roadmap synthesis."""
        text = ""
        try:
            text = orch.memory.read_roadmap()
        except Exception:
            text = ""
        return JSONResponse({"roadmap": text})

    @app.get("/api/suggestions")
    async def get_suggestions() -> JSONResponse:
        """Open suggestions/risks from background research — so the Home can show what Orrin
        found while idle, persistently (the live WS tiles are transient)."""
        items: list = []
        try:
            items = orch.memory.list_suggestions(status="open", limit=24)
        except Exception:
            items = []
        return JSONResponse({"items": items})

    # -- deck: live state for the Home constellation (all real, no static data) --
    @app.get("/api/deck")
    async def get_deck() -> JSONResponse:
        from . import data
        b = orch.settings.brain
        v = orch.settings.voice
        checks = {c.key: c.status for c in (orch.preflight_checks or [])}
        # Live workspaces (the real parallel Claude sessions).
        workspaces = [{"name": ws.name, "state": ws.state,
                       "focused": ws.project == orch.focused,
                       "active": ws.state in ("working", "thinking", "speaking"),
                       "desc": f"Workspace “{ws.name}” — a parallel Claude session, "
                               f"currently {ws.state}."}
                      for ws in orch.workspaces.values()]
        # Real memory size for this project.
        facts = 0
        try:
            pj = orch.memory.project_dir(orch.current_project) / "project.md"
            if pj.exists():
                facts = sum(1 for ln in pj.read_text().splitlines()
                            if ln.strip()[:2] in ("- ", "* "))
        except Exception:
            pass
        try:
            mcp = data.mcp_count(orch.focused)
        except Exception:
            mcp = 0
        remote = orch.remote_clients
        perm = b.permission
        # Each subsystem's status is derived from LIVE state, never hardcoded.
        subs = [
            {"key": "vox", "label": "VOX", "role": "Voice I/O",
             "status": (v.tts.engine if checks.get("voice_output") == "ok" else "off"),
             "level": "ok" if checks.get("voice_output") == "ok" else "idle",
             "desc": "Text-to-speech — the voice Orrin speaks replies with."},
            {"key": "ears", "label": "EARS", "role": "Speech",
             "status": (v.stt.model if checks.get("microphone") == "ok"
                        else "blocked" if checks.get("microphone") == "bad" else "off"),
             "level": "ok" if checks.get("microphone") == "ok" else "warn",
             "desc": "Speech-to-text — the Whisper model that hears you; needs the microphone."},
            {"key": "mnemo", "label": "MNEMO", "role": "Memory",
             "status": (f"{facts} facts" if facts else "empty"),
             "level": "ok" if facts else "idle",
             "desc": "Persistent memory — what Orrin remembers about this project across sessions."},
            {"key": "sentry", "label": "SENTRY", "role": "Permissions",
             "status": ("full access" if perm in ("bypass", "auto")
                        else "read-only" if perm == "deny" else "guarding"),
             "level": "ok",
             "desc": "Access guard — how much Orrin can do without asking. Change it in Settings."},
            {"key": "relay", "label": "RELAY", "role": "Mobile Link",
             "status": (f"{remote} live" if remote
                        else "ready" if orch.settings.server.allow_remote else "off"),
             "level": "ok" if remote else "idle", "active": bool(remote),
             "desc": ("Phone connected — talking to Orrin remotely." if remote
                      else "Mobile link — connect the phone app to talk from anywhere."
                      if orch.settings.server.allow_remote else "Mobile link is off.")},
            {"key": "nexus", "label": "NEXUS", "role": "Connectors",
             "status": (f"{mcp} active" if mcp else "none"),
             "level": "ok" if mcp else "idle",
             "desc": "MCP connectors — the external tools Claude can use in this session."},
        ]
        # All the user's Claude Code projects — the nodes of the hub-and-spoke graph.
        open_paths = {ws.project for ws in orch.workspaces.values()}
        projects = []
        try:
            for p in list_projects(current=orch.current_project,
                                   pinned=orch.settings.brain.projects, limit=12):
                if p.path in ("/", ""):
                    continue
                projects.append({
                    "name": p.name, "path": p.path, "current": p.current,
                    "active": p.current or p.path in open_paths,
                    "desc": ("The project Orrin is working in now." if p.current
                             else "Open workspace — a live Claude session." if p.path in open_paths
                             else f"Claude project · {p.path}")})
        except Exception:
            pass
        # Real 7-day activity (turns per day) for the current project — for the trend card.
        import time as _t
        activity = [0] * 7
        try:
            now = _t.time()
            tp = orch.memory.project_dir(orch.current_project) / "turns.jsonl"
            if tp.exists():
                for ln in tp.read_text().splitlines():
                    try:
                        ts = json.loads(ln).get("ts", 0)
                    except Exception:
                        continue
                    day = int((now - ts) // 86400)
                    if 0 <= day < 7:
                        activity[6 - day] += 1
        except Exception:
            pass
        stats = {"projects": len(projects), "connectors": mcp,
                 "workspaces": len(orch.workspaces), "facts": facts,
                 "activity": activity}
        return JSONResponse({
            "project": {"name": Path(orch.current_project).name,
                        "path": orch.current_project,
                        "claude": checks.get("claude_test") == "ok"},
            "projects": projects[:10], "stats": stats,
            "workspaces": workspaces, "subsystems": subs})

    # -- dictation-anywhere toggle ----------------------------------------
    @app.get("/api/dictation")
    async def get_dictation() -> JSONResponse:
        return JSONResponse({"on": orch._dictation})

    @app.post("/api/dictation")
    async def set_dictation(payload: dict) -> JSONResponse:
        orch.set_dictation(bool(payload.get("on", not orch._dictation)))
        return JSONResponse({"ok": True, "on": orch._dictation})

    # -- preflight / readiness (first-run permissions + health check) ------
    @app.get("/api/preflight")
    async def get_preflight() -> JSONResponse:
        return JSONResponse({
            "checks": [c.as_dict() for c in orch.preflight_checks],
            "ready": orch.ready,
        })

    @app.post("/api/preflight/recheck")
    async def recheck_preflight() -> JSONResponse:
        asyncio.create_task(orch.run_preflight(request=True))
        return JSONResponse({"ok": True})

    @app.post("/api/system/open-pane")
    async def open_pane(payload: dict) -> JSONResponse:
        which = payload.get("pane", "")
        ok = orch.open_privacy_pane(which)
        return JSONResponse({"ok": ok})

    # -- event stream ------------------------------------------------------
    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        # Reject cross-site / non-loopback sockets before accepting — a random web page or LAN
        # client must not be able to open this and drive the agent. A loopback dashboard passes
        # _peer_local + Origin (the peer address is checked, not just the forgeable Host); the
        # mobile app passes _authed with its bearer token (header or ?token=).
        local_ok = _peer_local(websocket) and _origin_ok(websocket.headers)
        is_remote = (not local_ok) and _authed(
            websocket.headers, websocket.query_params.get("token"))
        if not (local_ok or is_remote):
            await websocket.close(code=1008)   # policy violation
            return
        await websocket.accept()
        # Track connected phones so the Mac can stay quiet while you're driving it remotely.
        if is_remote:
            orch.remote_clients += 1
        queue = bus.subscribe()
        # Replay saved conversation so a reloaded/reopened dashboard shows history — the
        # focused project's own log, from the per-project memory store.
        history = orch.history.load(orch.current_project)
        if history:
            await websocket.send_json({"type": "history_start"})
            for ev in history:
                await websocket.send_json(ev)
            await websocket.send_json({"type": "history_end"})
        # Send the current project + chat + workspace pool immediately so a fresh page
        # shows context.
        orch._publish_project()
        orch._publish_session()
        orch._publish_workspaces()
        # Show the setup/health panel state (mic, Claude test run, …) to a fresh page.
        if orch.preflight_checks:
            orch._publish_preflight(done=orch.preflight_done)
        # Replay recent log lines so the Logs drawer isn't empty on a fresh page.
        for ev in list(log_buffer):
            await websocket.send_json(ev)

        async def pump() -> None:
            while True:
                event = await queue.get()
                await websocket.send_json(event)

        pump_task = asyncio.create_task(pump())
        try:
            while True:
                data = await websocket.receive_json()
                if data.get("type") == "user_text":
                    orch.submit_user_input(data.get("text", ""))
                elif data.get("type") == "stop":
                    orch.interrupt_now("button")
                elif data.get("type") == "ptt":
                    orch.arm_ptt()
                elif data.get("type") == "switch_project":
                    asyncio.create_task(orch.switch_project(data.get("path", "")))
                elif data.get("type") == "switch_session":
                    asyncio.create_task(orch.switch_session(data.get("session_id", "")))
                elif data.get("type") == "new_chat":
                    asyncio.create_task(orch.new_chat())
                elif data.get("type") == "open_session_anywhere":
                    asyncio.create_task(orch.open_session_anywhere(
                        data.get("project", ""), data.get("session_id", "")))
                elif data.get("type") == "rename_chat":
                    asyncio.create_task(orch.rename_chat(data.get("title", "")))
                elif data.get("type") == "open_workspace":
                    asyncio.create_task(orch.open_workspace(
                        data.get("path", ""), focus=data.get("focus", True)))
                elif data.get("type") == "focus_workspace":
                    orch.focus_workspace(data.get("path", ""))
                elif data.get("type") == "close_workspace":
                    asyncio.create_task(orch.close_workspace(data.get("path", "")))
                elif data.get("type") == "clone_workspace":
                    asyncio.create_task(orch.clone_workspace(
                        data.get("url", ""), data.get("branch")))
                elif data.get("type") == "recheck":
                    asyncio.create_task(orch.run_preflight(request=True))
                elif data.get("type") == "open_pane":
                    orch.open_privacy_pane(data.get("pane", ""))
                elif data.get("type") == "mute_mic":
                    orch.mic_muted = bool(data.get("value"))
                elif data.get("type") == "mute_voice":
                    orch.voice_muted = bool(data.get("value"))
                elif data.get("type") == "enroll_start":
                    orch.start_enrollment_named(data.get("name", ""), data.get("count"))
                elif data.get("type") == "enroll_cancel":
                    orch.cancel_enrollment()
                elif data.get("type") == "remove_voice":
                    orch.remove_voice(data.get("name", ""))
        except WebSocketDisconnect:
            pass
        finally:
            pump_task.cancel()
            bus.unsubscribe(queue)
            if is_remote:
                orch.remote_clients = max(0, orch.remote_clients - 1)

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    if mcp_app is not None:
        app.mount("/mcp", mcp_app)
    return app
