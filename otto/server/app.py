"""FastAPI app: dashboard + settings UI, a WebSocket event stream, a small config/projects
API, and (optionally) the Otto MCP server mounted at /mcp for Claude to connect to."""

from __future__ import annotations

import asyncio
from pathlib import Path

import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import config_path, save_config
from ..orchestrator import Orchestrator
from ..projects import list_projects
from ..sessions import list_sessions
from .events import EventBus

WEB_DIR = Path(__file__).parent / "web"


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

    app = FastAPI(title="Otto", lifespan=lifespan)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/favicon.ico")
    async def favicon() -> FileResponse:
        return FileResponse(WEB_DIR / "favicon-32.png")

    @app.get("/settings")
    async def settings_page() -> FileResponse:
        return FileResponse(WEB_DIR / "settings.html")

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

    # -- voices (pick Otto's spoken voice) --------------------------------
    @app.get("/api/voices")
    async def get_voices() -> JSONResponse:
        from ..voice.tts import list_system_voices
        return JSONResponse({
            "voices": list_system_voices(),
            "current": orch.settings.voice.tts.voice,
            "engine": orch.settings.voice.tts.engine,
        })

    @app.post("/api/voice/select")
    async def select_voice(payload: dict) -> JSONResponse:
        orch.set_voice(payload.get("voice", ""))
        return JSONResponse({"ok": True})

    @app.post("/api/voice/preview")
    async def preview_voice(payload: dict) -> JSONResponse:
        orch.preview_voice(payload.get("voice", ""))
        return JSONResponse({"ok": True})

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
        await websocket.accept()
        queue = bus.subscribe()
        # Replay saved conversation so a reloaded/reopened dashboard shows history.
        history = orch.history.load()
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
        except WebSocketDisconnect:
            pass
        finally:
            pump_task.cancel()
            bus.unsubscribe(queue)

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    if mcp_app is not None:
        app.mount("/mcp", mcp_app)
    return app
