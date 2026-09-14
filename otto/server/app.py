"""FastAPI app: dashboard + settings UI, a WebSocket event stream, a small config/projects
API, and (optionally) the Otto MCP server mounted at /mcp for Claude to connect to."""

from __future__ import annotations

import asyncio
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
    _ok_hosts = {f"127.0.0.1:{_port}", f"localhost:{_port}"}

    _srv = orch.settings.server
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

    def _origin_ok(headers) -> bool:
        if headers.get("host", "") not in _ok_hosts:
            return False
        origin = headers.get("origin")
        return origin is None or origin in _ok_origins

    @app.middleware("http")
    async def _guard(request, call_next):
        # Read-only GETs are harmless; guard everything that mutates state. A valid bearer token
        # (remote app) is accepted regardless of Origin — it can't be forged by a web page.
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if not (_origin_ok(request.headers)
                    or _authed(request.headers, request.query_params.get("token"))):
                return JSONResponse({"error": "forbidden: cross-origin request blocked"},
                                    status_code=403)
        return await call_next(request)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/favicon.ico")
    async def favicon() -> FileResponse:
        return FileResponse(WEB_DIR / "favicon-32.png")

    @app.get("/settings")
    async def settings_page() -> FileResponse:
        return FileResponse(WEB_DIR / "settings.html")

    # -- mobile pairing ---------------------------------------------------
    # What the phone needs to connect: the token + reachable addresses. Only served to a
    # LOOPBACK caller (the local dashboard / menu bar) so a remote client can't fetch the
    # secret it doesn't already have. Meant to back a "Pair phone" QR/details screen.
    @app.get("/api/pairing")
    async def get_pairing(request: Request) -> JSONResponse:
        if not _origin_ok(request.headers):
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

    # -- config + projects API --------------------------------------------
    @app.get("/api/config")
    async def get_config() -> JSONResponse:
        cfg = config_path()
        data = yaml.safe_load(cfg.read_text()) if cfg.exists() else {}
        return JSONResponse({"config": data or {}, "path": str(cfg)})

    @app.put("/api/config")
    async def put_config(payload: dict) -> JSONResponse:
        data = payload.get("config", payload)
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
        return JSONResponse({
            "project": {"name": Path(orch.current_project).name,
                        "path": orch.current_project,
                        "claude": checks.get("claude_test") == "ok"},
            "projects": projects[:10],
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
        # Reject cross-site sockets before accepting — a random web page must not be able to
        # open this and drive the agent (see the guard above). A loopback dashboard passes
        # _origin_ok; the mobile app passes _authed with its bearer token (header or ?token=).
        local_ok = _origin_ok(websocket.headers)
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
