"""The mouth: text-to-speech with barge-in.

Default engine is macOS **`say`** — native, reliable even from a launchd background app
(RealtimeTTS's pyttsx3 engine plays inconsistently there), and easy to interrupt. Set the
TTS engine to "elevenlabs" (with ELEVENLABS_API_KEY) for a premium streaming voice via
RealtimeTTS.

Sentences are queued and spoken in order on a worker thread, so `speak()` returns
immediately and `stop()` cuts off playback mid-sentence for barge-in.
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import time

from ..config import Settings


class Mouth:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._engine = settings.voice.tts.engine
        self._voice = settings.voice.tts.voice
        # macOS `say` path
        self._q: "queue.Queue[str | None]" = queue.Queue()
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._last_end = 0.0   # when the last utterance finished (for echo suppression)
        # ElevenLabs (RealtimeTTS) path
        self._stream = None

    def start(self) -> None:
        if self._engine == "elevenlabs":
            self._start_elevenlabs()
        else:
            self._worker = threading.Thread(target=self._say_loop, daemon=True)
            self._worker.start()

    # -- macOS say ---------------------------------------------------------
    def _say_loop(self) -> None:
        while True:
            text = self._q.get()
            if text is None:
                return
            argv = ["/usr/bin/say"]
            if self._voice:
                argv += ["-v", self._voice]
            argv.append(text)
            try:
                with self._lock:
                    self._proc = subprocess.Popen(argv)
                rc = self._proc.wait()
                print(f"[otto] say rc={rc} text={text[:40]!r}", file=sys.stderr, flush=True)
            except Exception as e:
                print(f"[otto] say FAILED: {e!r}", file=sys.stderr, flush=True)
            finally:
                with self._lock:
                    self._proc = None
                if self._q.empty():
                    self._last_end = time.monotonic()

    def speak(self, text: str) -> None:
        if not text:
            return
        if self._engine == "elevenlabs" and self._stream is not None:
            self._stream.feed(text)
            if not self._stream.is_playing():
                self._stream.play_async()
            return
        self._q.put(text)

    def stop(self) -> None:
        """Barge-in: drop anything queued and cut the current utterance."""
        if self._engine == "elevenlabs" and self._stream is not None:
            try:
                self._stream.stop()
            except Exception:
                pass
            return
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass
        with self._lock:
            if self._proc is not None:
                try:
                    self._proc.terminate()
                except Exception:
                    pass

    def is_playing(self) -> bool:
        if self._engine == "elevenlabs" and self._stream is not None:
            return bool(self._stream.is_playing())
        with self._lock:
            return self._proc is not None or not self._q.empty()

    def recently_active(self, window: float = 1.5) -> bool:
        """Playing, or finished within `window`s — used to ignore the mic hearing Otto."""
        if self.is_playing():
            return True
        return (time.monotonic() - self._last_end) < window

    # -- ElevenLabs (RealtimeTTS) ------------------------------------------
    def _start_elevenlabs(self) -> None:
        import os

        from RealtimeTTS import ElevenlabsEngine, TextToAudioStream

        kwargs = {"api_key": os.environ.get("ELEVENLABS_API_KEY", "")}
        if self._voice:
            kwargs["voice"] = self._voice
        self._stream = TextToAudioStream(ElevenlabsEngine(**kwargs))
