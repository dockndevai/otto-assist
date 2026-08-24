"""Jarvis entrypoint: start the brain + voice loop and serve the dashboard."""

from __future__ import annotations

import asyncio
import webbrowser

import uvicorn

from jarvis.config import load_env, load_settings
from jarvis.orchestrator import Orchestrator
from jarvis.server.app import create_app
from jarvis.server.events import EventBus


async def main_async() -> None:
    load_env()
    settings = load_settings()
    bus = EventBus()
    orch = Orchestrator(settings, bus)
    app = create_app(orch, bus)

    await orch.start()

    url = f"http://{settings.server.host}:{settings.server.port}"
    print(f"\n  Jarvis dashboard → {url}\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    config = uvicorn.Config(
        app,
        host=settings.server.host,
        port=settings.server.port,
        log_level="warning",
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        await orch.stop()


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\n  Jarvis shutting down.")


if __name__ == "__main__":
    main()
