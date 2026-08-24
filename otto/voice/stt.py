"""The ears: local speech-to-text with wake word + VAD, via RealtimeSTT (faster-whisper).

Runs the blocking recorder loop on a background thread and hands final/partial
transcripts back to the asyncio loop through thread-safe callbacks. A wake-word or
speech-start event triggers barge-in and the "listening" indicator.

Wake word:
- "oww" (openWakeWord) uses built-in phrases only ("hey jarvis", …).
- "pvporcupine" supports a CUSTOM keyword (e.g. "hey otto") via a .ppn file: set
  `wake_words` to the .ppn path and put your Picovoice key in PICOVOICE_ACCESS_KEY.
  RealtimeSTT itself only passes built-in keyword *names*, so we install a small shim on
  `pvporcupine.create` that redirects .ppn paths to `keyword_paths` and injects the key.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Callable

from ..config import Settings


def _install_porcupine_custom_keyword_shim() -> None:
    """Let RealtimeSTT use a custom .ppn keyword + access key with Porcupine.

    RealtimeSTT calls ``pvporcupine.create(keywords=[...], sensitivities=[...])``. We wrap
    it so any entry ending in ``.ppn`` becomes a ``keyword_paths`` entry, and the access
    key is supplied from PICOVOICE_ACCESS_KEY when not passed.
    """
    import pvporcupine

    if getattr(pvporcupine.create, "_otto_shim", False):
        return
    _orig = pvporcupine.create

    def create(**kwargs):
        kwargs.setdefault("access_key", os.environ.get("PICOVOICE_ACCESS_KEY", ""))
        kws = list(kwargs.get("keywords") or [])
        paths = [k for k in kws if str(k).endswith(".ppn")]
        names = [k for k in kws if not str(k).endswith(".ppn")]
        if paths:
            kwargs["keyword_paths"] = paths
            if names:
                kwargs["keywords"] = names
            else:
                kwargs.pop("keywords", None)
        return _orig(**kwargs)

    create._otto_shim = True  # type: ignore[attr-defined]
    pvporcupine.create = create


class Ears:
    def __init__(
        self,
        settings: Settings,
        loop,
        on_final: Callable[[str], None],
        on_partial: Callable[[str], None],
        on_speech_start: Callable[[], None],
        on_wake: Callable[[], None],
        on_recording_stop: Callable[[], None] | None = None,
    ) -> None:
        self.settings = settings
        self._loop = loop
        self._on_final = on_final
        self._on_partial = on_partial
        self._on_speech_start = on_speech_start
        self._on_wake = on_wake
        self._on_recording_stop = on_recording_stop
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
            level=logging.WARNING,           # quiet RealtimeSTT's very chatty logging
            # Live-partial transcription spawns a second worker that spams EOFErrors and
            # burns CPU on an always-on app; we only need final transcripts for the wake
            # phrase, so keep it off.
            enable_realtime_transcription=False,
            on_recording_start=lambda: self._post(self._on_speech_start),
        )
        if self._on_recording_stop is not None:
            params["on_recording_stop"] = lambda: self._post(self._on_recording_stop)

        # "phrase" = software wake word: transcribe everything (continuous VAD) and let the
        # orchestrator match the phrase. Otherwise use a hardware wake-word gate.
        use_gate = bool(cfg.wake_words) and cfg.wakeword_backend != "phrase"
        if use_gate:
            if cfg.wakeword_backend in ("pvporcupine", "porcupine", "pvp"):
                _install_porcupine_custom_keyword_shim()
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
