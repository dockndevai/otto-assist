"""Few-shot custom wake word via EfficientWord-Net.

Anyone can make their own wake word by recording their phrase a few times
(`python -m otto.enroll_wake_word`) — no training, no account. This runs the resulting
detector on its own mic stream (coexists with RealtimeSTT) and calls `on_wake` when it
hears the phrase; the orchestrator then arms capture of the command.
"""

from __future__ import annotations

import threading
from typing import Callable


class EffWakeWord:
    def __init__(
        self,
        reference_file: str,
        hotword: str,
        threshold: float,
        loop,
        on_wake: Callable[[], None],
    ) -> None:
        self.reference_file = reference_file
        self.hotword = hotword
        self.threshold = threshold
        self._loop = loop
        self._on_wake = on_wake
        self._detector = None
        self._stream = None
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self) -> None:
        from eff_word_net.audio_processing import Resnet50_Arc_loss  # lazy imports
        from eff_word_net.engine import HotwordDetector
        from eff_word_net.streams import SimpleMicStream

        model = Resnet50_Arc_loss()
        self._detector = HotwordDetector(
            hotword=self.hotword,
            model=model,
            reference_file=self.reference_file,
            threshold=self.threshold,
            relaxation_time=2.0,
        )
        self._stream = SimpleMicStream(window_length_secs=1.5, sliding_window_secs=0.75)
        self._stream.start_stream()
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while self._running:
            try:
                frame = self._stream.getFrame()
                result = self._detector.scoreFrame(frame)
            except Exception:
                continue
            if result and result.get("match"):
                self._loop.call_soon_threadsafe(self._on_wake)

    def stop(self) -> None:
        self._running = False
        if self._stream is not None:
            try:
                self._stream.close_stream()
            except Exception:
                pass
