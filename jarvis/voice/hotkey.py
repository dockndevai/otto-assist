"""Global push-to-talk hotkey (pynput).

Hold the configured chord to talk; release to send. Callbacks are bounced onto the
asyncio loop so they're safe to use with the rest of the app.
"""

from __future__ import annotations

from typing import Callable

from ..config import Settings


class PushToTalk:
    def __init__(
        self,
        settings: Settings,
        loop,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
    ) -> None:
        self.settings = settings
        self._loop = loop
        self._on_press = on_press
        self._on_release = on_release
        self._listener = None
        self._held = False

    def start(self) -> None:
        from pynput import keyboard  # lazy import

        chord = self.settings.voice.hotkey.push_to_talk
        hotkey = keyboard.HotKey(keyboard.HotKey.parse(chord), self._pressed)
        # canonical() maps modifier variants so the chord matches reliably.
        def on_press(key):
            hotkey.press(self._listener.canonical(key))

        def on_release(key):
            hotkey.release(self._listener.canonical(key))
            if self._held:
                self._held = False
                self._loop.call_soon_threadsafe(self._on_release)

        self._listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self._listener.start()

    def _pressed(self) -> None:
        if not self._held:
            self._held = True
            self._loop.call_soon_threadsafe(self._on_press)

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
