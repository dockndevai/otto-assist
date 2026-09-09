"""Otto entrypoint: start the brain + voice loop and serve the dashboard."""

from __future__ import annotations

import asyncio
import webbrowser

import uvicorn

from otto.config import load_env, load_settings
from otto.orchestrator import Orchestrator
from otto.server.app import create_app
from otto.server.events import EventBus


async def main_async() -> None:
    from otto.paths import augment_path
    augment_path()   # so node/npx-based MCP tools (browser, computer-use) can be spawned
    load_env()
    settings = load_settings()
    bus = EventBus()
    orch = Orchestrator(settings, bus)
    app = create_app(orch, bus)

    await orch.start()

    url = f"http://{settings.server.host}:{settings.server.port}"
    print(f"\n  Otto dashboard → {url}\n")
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
        print("\n  Otto shutting down.")


if __name__ == "__main__":
    main()
