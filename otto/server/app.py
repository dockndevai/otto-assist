"""FastAPI app: serves the dashboard and a WebSocket that streams events to the browser
and accepts typed input back."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..orchestrator import Orchestrator
from .events import EventBus

WEB_DIR = Path(__file__).parent / "web"


def create_app(orch: Orchestrator, bus: EventBus) -> FastAPI:
    app = FastAPI(title="Otto")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        queue = bus.subscribe()

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
        except WebSocketDisconnect:
            pass
        finally:
            pump_task.cancel()
            bus.unsubscribe(queue)

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app
