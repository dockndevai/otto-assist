"""NativeEars — Orrin's ears backed by the native `orrin-audio` helper (AVAudioEngine +
VoiceProcessingIO acoustic echo cancellation).

Exposes the same contract the orchestrator uses from `Ears` (the injected callbacks + `set_mic` +
`last_audio` + `start`/`stop`), so `_start_ears` can swap it in. The helper streams echo-cancelled
16 kHz int16 utterance PCM over stdio; we transcribe it here reusing the MLX path.

AEC readiness is an explicit handshake: the helper reports `{"type":"ready","aec":<bool>}`, and
full-duplex is enabled by the orchestrator ONLY when `aec` is true. Native-helper trouble (handshake
timeout, unexpected exit, engine error) fires `on_native_failed(reason)` — DISTINCT from a dead
microphone — so the orchestrator can fall back to RealtimeSTT. `on_health("deaf")` is reserved for a
genuinely dead recorder. Off by default (`voice.stt.native_audio`).
"""
from __future__ import annotations

import collections
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable

_READY_TIMEOUT = 5.0   # seconds to wait for the helper's AEC-readiness handshake


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
                 on_health: Callable | None = None, on_ready: Callable | None = None,
                 on_native_failed: Callable | None = None) -> None:
        self.settings = settings
        self._loop = loop
        self._on_final = on_final
        self._on_speech_start = on_speech_start
        self._on_recording_stop = on_recording_stop
        self._on_realtime = on_realtime
        self._on_health = on_health
        self._on_ready = on_ready                 # on_ready(aec: bool) — AEC handshake result
        self._on_native_failed = on_native_failed  # on_native_failed(reason: str) — helper trouble
        self.sample_rate = 16000
        self.last_audio = b""
        self.aec_ok: bool | None = None           # None until the readiness handshake completes
        self._bin = helper_binary_path()
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._stderr_tail: "collections.deque[str]" = collections.deque(maxlen=50)
        self._stop = threading.Event()
        self._expected_stop = False               # set on deliberate stop() — suppress failure cbs
        self._ready_seen = False
        self._ready_timer: threading.Timer | None = None
        self._failed_fired = False                # fire on_native_failed at most once
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
            stderr=subprocess.PIPE, bufsize=0)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._stderr_reader = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_reader.start()
        # Fail closed if the helper never reports AEC readiness (hung engine / no input device).
        self._ready_timer = threading.Timer(_READY_TIMEOUT, self._on_ready_timeout)
        self._ready_timer.daemon = True
        self._ready_timer.start()

    def stop(self) -> None:
        """Deliberate shutdown. Joins the helper + reader so no native audio tap is still live when
        this returns (single-mic-owner invariant on fallback). Marks the stop expected so a late
        exit/error from the reader loop can't fire on_native_failed."""
        self._expected_stop = True
        self._stop.set()
        if self._ready_timer is not None:
            self._ready_timer.cancel()
        self._send({"cmd": "quit"})
        p, self._proc = self._proc, None
        if p is not None:
            try:
                p.terminate()
            except Exception:
                pass
            try:
                p.wait(timeout=3.0)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        for th in (self._reader, self._stderr_reader):
            if th is not None and th.is_alive():
                try:
                    th.join(timeout=3.0)
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

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_tail)

    # ---- internals -----------------------------------------------------------
    def _post(self, fn: Callable | None, *args: Any) -> None:
        if fn is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(lambda: fn(*args))

    def _fail(self, reason: str) -> None:
        """Signal native-helper failure exactly once, unless the stop was expected."""
        if self._expected_stop or self._failed_fired:
            return
        self._failed_fired = True
        if self._ready_timer is not None:
            self._ready_timer.cancel()
        print(f"[otto] native-ears failed ({reason}); stderr: {self.stderr_tail()[-300:]!r}",
              file=sys.stderr, flush=True)
        self._post(self._on_native_failed, reason)

    def _on_ready_timeout(self) -> None:
        if not self._ready_seen:
            self._fail("timeout")

    def _send(self, obj: dict) -> None:
        try:
            if self._proc is not None and self._proc.stdin is not None:
                self._proc.stdin.write((json.dumps(obj) + "\n").encode())
                self._proc.stdin.flush()
        except Exception:
            pass

    def _drain_stderr(self) -> None:
        f = self._proc.stderr if self._proc is not None else None
        if f is None:
            return
        try:
            for raw in iter(f.readline, b""):
                self._stderr_tail.append(raw.decode("utf-8", "replace").rstrip())
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
            if t == "ready":
                self._ready_seen = True
                self.aec_ok = bool(msg.get("aec"))
                if self._ready_timer is not None:
                    self._ready_timer.cancel()
                self._post(self._on_ready, self.aec_ok)
            elif t == "speech_start":
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
                self._fail("engine_error")
        if not self._stop.is_set():                    # helper exited unexpectedly
            self._fail("helper_exit")
