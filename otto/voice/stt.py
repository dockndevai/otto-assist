"""The ears: local speech-to-text with wake word + VAD, via RealtimeSTT (faster-whisper).

Runs the blocking recorder loop on a background thread and hands final/partial
transcripts back to the asyncio loop through thread-safe callbacks. A wake-word or
speech-start event is what triggers barge-in (stopping the mouth mid-sentence).
"""

from __future__ import annotations

import threading
from typing import Callable

from ..config import Settings


class Ears:
    def __init__(
        self,
        settings: Settings,
        loop,
        on_final: Callable[[str], None],
        on_partial: Callable[[str], None],
        on_speech_start: Callable[[], None],
        on_wake: Callable[[], None],
    ) -> None:
        self.settings = settings
        self._loop = loop
        self._on_final = on_final
        self._on_partial = on_partial
        self._on_speech_start = on_speech_start
        self._on_wake = on_wake
        self._recorder = None
        self._thread: threading.Thread | None = None
        self._running = False

    def _post(self, fn, *args) -> None:
        """Bounce a callback from the recorder thread onto the asyncio loop."""
        self._loop.call_soon_threadsafe(fn, *args)

    def start(self) -> None:
        from RealtimeSTT import AudioToTextRecorder  # lazy import

        cfg = self.settings.voice.stt
        params = dict(
            model=cfg.model,
            language=cfg.language,
            spinner=False,
            enable_realtime_transcription=True,
            on_realtime_transcription_update=lambda t: self._post(self._on_partial, t),
            on_recording_start=lambda: self._post(self._on_speech_start),
        )
        # Wake word is optional; when set, the recorder idles until it hears it.
        if cfg.wake_words:
            params.update(
                wakeword_backend=cfg.wakeword_backend,
                wake_words=cfg.wake_words,
                wake_words_sensitivity=cfg.wake_word_sensitivity,
                on_wakeword_detected=lambda: self._post(self._on_wake),
            )

        self._recorder = AudioToTextRecorder(**params)
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while self._running:
            try:
                text = self._recorder.text()  # blocks: (wake ->) record -> transcribe
            except Exception:
                continue
            if text and self._running:
                self._post(self._on_final, text.strip())

    def trigger_ptt_start(self) -> None:
        """Push-to-talk pressed: begin capturing immediately (bypass wake word)."""
        if self._recorder is not None:
            try:
                self._recorder.start()
            except Exception:
                pass

    def trigger_ptt_stop(self) -> None:
        """Push-to-talk released: end capture and let it transcribe."""
        if self._recorder is not None:
            try:
                self._recorder.stop()
            except Exception:
                pass

    def stop(self) -> None:
        self._running = False
        if self._recorder is not None:
            try:
                self._recorder.shutdown()
            except Exception:
                pass
