"""The mouth: streaming text-to-speech with barge-in.

Wraps RealtimeTTS. Default engine is the local macOS system voice (no key). Set the TTS
engine to "elevenlabs" (with ELEVENLABS_API_KEY) for a premium voice.

Speech runs on RealtimeTTS's own playback thread, so `speak()` returns immediately and
`stop()` can cut it off mid-sentence when the user starts talking (barge-in).
"""

from __future__ import annotations

import os

from ..config import Settings


class Mouth:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._stream = None  # RealtimeTTS TextToAudioStream

    def start(self) -> None:
        from RealtimeTTS import TextToAudioStream  # lazy import

        engine = self._make_engine()
        self._stream = TextToAudioStream(engine)

    def _make_engine(self):
        cfg = self.settings.voice.tts
        if cfg.engine == "elevenlabs":
            from RealtimeTTS import ElevenlabsEngine

            api_key = os.environ.get("ELEVENLABS_API_KEY", "")
            kwargs = {"api_key": api_key}
            if cfg.voice:
                kwargs["voice"] = cfg.voice
            return ElevenlabsEngine(**kwargs)

        # Default: local system voice (macOS `say` via RealtimeTTS SystemEngine).
        from RealtimeTTS import SystemEngine

        return SystemEngine()

    def speak(self, text: str) -> None:
        """Queue text and start (or continue) playback without blocking."""
        if not text or self._stream is None:
            return
        self._stream.feed(text)
        if not self._stream.is_playing():
            self._stream.play_async()

    def stop(self) -> None:
        """Barge-in: stop playback immediately and clear the queue."""
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception:
                pass

    def is_playing(self) -> bool:
        return bool(self._stream and self._stream.is_playing())
