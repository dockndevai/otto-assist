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
from .events import EventBus

WEB_DIR = Path(__file__).parent / "web"


def create_app(orch: Orchestrator, bus: EventBus) -> FastAPI:
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

    # -- event stream ------------------------------------------------------
    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        queue = bus.subscribe()
        # Send the current project immediately so a fresh page shows context.
        orch._publish_project()

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
                elif data.get("type") == "switch_project":
                    asyncio.create_task(orch.switch_project(data.get("path", "")))
        except WebSocketDisconnect:
            pass
        finally:
            pump_task.cancel()
            bus.unsubscribe(queue)

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    if mcp_app is not None:
        app.mount("/mcp", mcp_app)
    return app
