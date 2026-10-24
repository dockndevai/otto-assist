"""NativeEars — Orrin's ears backed by the native `orrin-audio` helper (AVAudioEngine +
VoiceProcessingIO acoustic echo cancellation).

Exposes the same contract the orchestrator uses from `Ears` (the injected callbacks + `set_mic` +
`last_audio` + `start`/`stop`), so `_start_ears` can swap it in. The helper streams echo-cancelled
16 kHz int16 utterance PCM over stdio; we transcribe it here reusing the MLX path. Because the AEC
removes Orrin's own voice from the mic, the orchestrator stops half-duplex muting while active →
full-duplex / barge-in on the built-in speakers. Off by default (`voice.stt.native_audio`).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable


def helper_binary_path() -> str | None:
    """Locate the compiled `orrin-audio` binary (dev build, packaged resource, or env override)."""
    cands: list[Path] = []
    env = os.environ.get("ORRIN_AUDIO_BIN")
    if env:
        cands.append(Path(env))
    repo = Path(__file__).resolve().parents[2]            # otto/voice/native_ears.py → repo root
    base = repo / "native" / "orrin-audio" / ".build"
    cands += [base / "release" / "orrin-audio",
              base / "arm64-apple-macosx" / "release" / "orrin-audio"]
    meipass = getattr(sys, "_MEIPASS", None)              # packaged app (PyInstaller)
    if meipass:
        cands.append(Path(meipass) / "orrin-audio")
    cands.append(Path(sys.executable).resolve().parent / "orrin-audio")
    for c in cands:
        try:
            if c.exists() and os.access(c, os.X_OK):
                return str(c)
        except OSError:
            continue
    return None


def _readexact(f, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = f.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


class NativeEars:
    def __init__(self, settings: Any, loop: Any, *,
                 on_final: Callable | None = None, on_partial: Callable | None = None,
                 on_speech_start: Callable | None = None, on_wake: Callable | None = None,
                 on_recording_stop: Callable | None = None, on_realtime: Callable | None = None,
                 on_health: Callable | None = None) -> None:
        self.settings = settings
        self._loop = loop
        self._on_final = on_final
        self._on_speech_start = on_speech_start
        self._on_recording_stop = on_recording_stop
        self._on_realtime = on_realtime
        self._on_health = on_health
        self.sample_rate = 16000
        self.last_audio = b""
        self._bin = helper_binary_path()
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self._mlx = None
        self._mlx_model = ""
        self._mlx_lang: str | None = None

    @staticmethod
    def available() -> bool:
        return helper_binary_path() is not None

    # ---- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if self._bin is None:
            raise RuntimeError("orrin-audio helper not found (build native/orrin-audio)")
        self._init_transcriber()
        self._proc = subprocess.Popen(
            [self._bin], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def stop(self) -> None:
        self._stop.set()
        self._send({"cmd": "quit"})
        p, self._proc = self._proc, None
        if p is not None:
            try:
                p.terminate()
            except Exception:
                pass

    def set_mic(self, on: bool) -> None:
        self._send({"cmd": "unmute" if on else "mute"})

    # PTT isn't wired for the native path in v1 (it's VAD/always-listen); no-op stubs keep the
    # orchestrator's Ears-shaped calls safe.
    def trigger_ptt_start(self) -> None:
        pass

    def trigger_ptt_stop(self) -> None:
        pass

    # ---- internals -----------------------------------------------------------
    def _post(self, fn: Callable | None, *args: Any) -> None:
        if fn is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(lambda: fn(*args))

    def _send(self, obj: dict) -> None:
        try:
            if self._proc is not None and self._proc.stdin is not None:
                self._proc.stdin.write((json.dumps(obj) + "\n").encode())
                self._proc.stdin.flush()
        except Exception:
            pass

    def _init_transcriber(self) -> None:
        try:
            import mlx_whisper
            from .stt import _resolve_lang, pick_mlx_model
            cfg = self.settings.voice.stt
            _, lang = _resolve_lang(self.settings.voice, cfg)
            self._mlx = mlx_whisper
            self._mlx_model = pick_mlx_model(cfg, getattr(self.settings.voice, "language", "en"))
            self._mlx_lang = None if (not lang or lang == "auto") else lang
        except Exception as e:
            self._mlx = None
            print(f"[otto] native-ears: MLX transcriber unavailable ({e!r})",
                  file=sys.stderr, flush=True)

    def _transcribe(self, pcm: bytes) -> str:
        if not pcm or self._mlx is None:
            return ""
        import numpy as np
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if audio.size < 1600:
            return ""
        kw = {"path_or_hf_repo": self._mlx_model}
        if self._mlx_lang:
            kw["language"] = self._mlx_lang
        text = (self._mlx.transcribe(audio, **kw).get("text") or "").strip()
        try:
            import mlx.core as mx
            (getattr(mx, "clear_cache", None)
             or getattr(getattr(mx, "metal", None), "clear_cache", lambda: None))()
        except Exception:
            pass
        return text

    def _read_loop(self) -> None:
        f = self._proc.stdout if self._proc is not None else None
        if f is None:
            return
        while not self._stop.is_set():
            line = f.readline()
            if not line:
                break
            try:
                msg = json.loads(line.decode("utf-8", "replace").strip())
            except Exception:
                continue
            t = msg.get("type")
            if t == "speech_start":
                self._post(self._on_speech_start)
            elif t == "utterance":
                pcm = _readexact(f, int(msg.get("bytes", 0) or 0))
                self.last_audio = pcm
                self._post(self._on_recording_stop)
                text = self._transcribe(pcm)
                if text:
                    self._post(self._on_final, text)
            elif t == "partial" and self._on_realtime is not None:
                self._post(self._on_realtime, msg.get("text", ""))
            elif t == "error":
                self._post(self._on_health, "deaf")
        if not self._stop.is_set():                    # helper exited unexpectedly
            self._post(self._on_health, "deaf")
