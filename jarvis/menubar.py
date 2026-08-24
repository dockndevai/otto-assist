"""Jarvis as a macOS menu bar app.

The AppKit run loop needs the main thread, so rumps runs there while the assistant
(orchestrator + brain + dashboard server) runs on its own asyncio loop in a background
thread. Menu actions on the main thread poke that loop via thread-safe calls; a 1s timer
reads the orchestrator's live state to keep the menu label current.

Run:  python -m jarvis.menubar
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import webbrowser

import rumps
import uvicorn

from .config import load_env, load_settings
from .orchestrator import Orchestrator
from .server.app import create_app
from .server.events import EventBus

_STATE_LABELS = {
    "starting": "Starting…",
    "idle": "● Ready",
    "listening": "● Listening…",
    "thinking": "● Thinking…",
    "speaking": "● Speaking…",
    "working": "● Working…",
}


class JarvisApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("Jarvis", title="🧠", quit_button=None)
        self.settings = load_settings()
        self.bus = EventBus()
        self.orch = Orchestrator(self.settings, self.bus)
        self.url = f"http://{self.settings.server.host}:{self.settings.server.port}"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: uvicorn.Server | None = None

        self.status_item = rumps.MenuItem("Starting…")
        self.mic_item = rumps.MenuItem("Mute microphone", callback=self.toggle_mic)
        self.voice_item = rumps.MenuItem("Mute voice", callback=self.toggle_voice)
        self.menu = [
            self.status_item,
            None,
            rumps.MenuItem("Open Dashboard", callback=self.open_dashboard),
            self.mic_item,
            self.voice_item,
            None,
            rumps.MenuItem("Restart", callback=self.restart),
            rumps.MenuItem("Quit Jarvis", callback=self.quit_app),
        ]

        threading.Thread(target=self._run_backend, daemon=True).start()

    # -- backend (its own asyncio loop, off the main thread) ---------------
    def _run_backend(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._backend_main())
        except Exception as e:  # surface fatal backend errors to the menu
            self.orch.state = f"error: {e}"

    async def _backend_main(self) -> None:
        await self.orch.start()
        config = uvicorn.Config(
            create_app(self.orch, self.bus),
            host=self.settings.server.host,
            port=self.settings.server.port,
            log_level="warning",
            loop="asyncio",
        )
        self._server = uvicorn.Server(config)
        # We're not on the main thread, so uvicorn can't install signal handlers.
        self._server.install_signal_handlers = lambda: None
        await self._server.serve()

    # -- menu (main thread) ------------------------------------------------
    @rumps.timer(1)
    def _tick(self, _) -> None:
        self.status_item.title = _STATE_LABELS.get(self.orch.state, self.orch.state)
        self.title = "🔇" if self.orch.mic_muted else "🧠"

    def open_dashboard(self, _) -> None:
        webbrowser.open(self.url)

    def toggle_mic(self, _) -> None:
        self.orch.mic_muted = not self.orch.mic_muted
        self.mic_item.title = "Unmute microphone" if self.orch.mic_muted else "Mute microphone"

    def toggle_voice(self, _) -> None:
        self.orch.voice_muted = not self.orch.voice_muted
        self.voice_item.title = "Unmute voice" if self.orch.voice_muted else "Mute voice"

    def restart(self, _) -> None:
        os.execv(sys.executable, [sys.executable, "-m", "jarvis.menubar"])

    def quit_app(self, _) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._loop is not None:
            self._loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(self.orch.stop())
            )
        rumps.quit_application()


def main() -> None:
    load_env()
    JarvisApp().run()


if __name__ == "__main__":
    main()
