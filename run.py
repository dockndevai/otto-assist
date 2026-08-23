"""Jarvis entrypoint: start the brain + voice loop and serve the dashboard."""

from __future__ import annotations

import asyncio
import os
import webbrowser
from pathlib import Path

import uvicorn

from jarvis.config import PROJECT_ROOT, load_settings
from jarvis.orchestrator import Orchestrator
from jarvis.server.app import create_app
from jarvis.server.events import EventBus


def load_env() -> None:
    """Minimal .env loader (KEY=VALUE lines) into os.environ, without a dependency."""
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


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
