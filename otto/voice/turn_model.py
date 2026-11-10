"""Smart Turn v3 — a learned, LOCAL end-of-turn detector (the real analogue of semantic VAD).

The regex `Endpointer` in `endpointer.py` *guesses* whether you've finished from the partial text
(terminal punctuation, a trailing "and", a hold phrase). This module replaces that guess with the
same CLASS of component OpenAI's `semantic_vad` uses: a tiny model that looks at the user's **audio
so far** and returns P(the turn is complete). We use Pipecat's Smart Turn v3 — a Whisper-Tiny encoder
+ a shallow classifier head, ~8 MB INT8 ONNX, BSD-2 licensed — which runs on the onnxruntime we
already ship for Silero VAD (no new heavy dependency) in ~15-40 ms on Apple Silicon.

Audio in, probability out. No network, no cloud, no engine round-trip. The preprocessing mirrors the
upstream reference exactly (Whisper feature extractor, last 8 s of audio, `input_features` tensor →
sigmoid). `prob_to_end_silence` maps the probability onto the same min/base/max end-silence windows
the regex endpointer uses, so the native helper's VAD is driven identically either way.

Fail-safe by construction: if onnxruntime, the Whisper feature extractor, or the model file is
missing — or inference errors repeatedly — `load()`/`target_for()` report unavailable and the caller
falls back to the regex `Endpointer`. Nothing here ever raises into the audio path.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from ..config import OTTO_HOME

_SAMPLE_RATE = 16000
_WINDOW_S = 8
_MIN_SAMPLES = _SAMPLE_RATE // 4         # need ~0.25 s of audio before a prediction means anything
_MAX_ERRORS = 3                          # disable the model after this many consecutive failures


def prob_to_end_silence(prob: float, *, min_ms: int, base_ms: int, max_ms: int,
                        high: float = 0.65, low: float = 0.35) -> tuple[int, str]:
    """Map P(turn complete) onto an end-of-turn silence window + a reason tag.

    High probability → snap to the snappy `min_ms` (commit fast); low → the patient `max_ms` (the
    speaker is mid-thought, don't cut them off); in between → the neutral `base_ms`. Pure + testable.
    """
    p = max(0.0, min(1.0, float(prob)))
    if p >= high:
        return int(min_ms), "complete"
    if p <= low:
        return int(max_ms), "unfinished"
    return int(base_ms), "neutral"


def turn_model_dir(settings: Any) -> Path:
    """Where the Smart Turn model file lives (config override, else OTTO_HOME/models/smart-turn)."""
    d = getattr(settings.voice.stt, "turn_model_dir", "") or ""
    return Path(d).expanduser() if d else (OTTO_HOME / "models" / "smart-turn")


def resolve_model_path(settings: Any) -> Path | None:
    """Resolve the configured `turn_model` to an existing .onnx file, or None (→ regex fallback).

    "off" → None. An explicit path → that file if it exists. "auto" → the first *.onnx in the model
    dir, if any.
    """
    mode = (getattr(settings.voice.stt, "turn_model", "auto") or "auto").strip()
    low = mode.lower()
    if low == "off":
        return None
    if low != "auto":
        p = Path(mode).expanduser()
        return p if p.exists() else None
    d = turn_model_dir(settings)
    if not d.exists():
        return None
    cands = sorted(d.glob("*.onnx"))
    return cands[0] if cands else None


class SmartTurnModel:
    """Lazy-loaded Smart Turn v3 ONNX session. `load()` is safe to call from any thread and returns
    whether the model is usable; `target_for(pcm)` returns an (end_silence_ms, reason) decision or
    None (too little audio, or the model is unavailable)."""

    def __init__(self, model_path: Path, *, min_ms: int = 250, base_ms: int = 500, max_ms: int = 900,
                 high: float = 0.65, low: float = 0.35) -> None:
        self.model_path = Path(model_path)
        self.min_ms, self.base_ms, self.max_ms = int(min_ms), int(base_ms), int(max_ms)
        self.high, self.low = float(high), float(low)
        self._session: Any = None
        self._feat: Any = None
        self._np: Any = None
        self._loaded = False          # load() has run (ok or not)
        self._ok = False              # session + feature extractor are usable
        self._errors = 0

    @property
    def ok(self) -> bool:
        return self._ok and self._errors < _MAX_ERRORS

    def load(self) -> bool:
        """Import deps + build the ONNX session and Whisper feature extractor. Idempotent; never
        raises. Returns True iff the model is ready to predict."""
        if self._loaded:
            return self.ok
        self._loaded = True
        try:
            import numpy as np
            import onnxruntime as ort
            from transformers import WhisperFeatureExtractor

            if not self.model_path.exists():
                raise FileNotFoundError(self.model_path)
            so = ort.SessionOptions()
            so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            so.inter_op_num_threads = 1
            so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            self._session = ort.InferenceSession(str(self.model_path), sess_options=so)
            self._feat = WhisperFeatureExtractor(chunk_length=_WINDOW_S)
            self._np = np
            self._ok = True
        except Exception as e:                       # missing dep / bad file / unsupported op
            print(f"[otto] smart-turn model unavailable ({e!r}); using regex endpointer",
                  file=sys.stderr, flush=True)
            self._ok = False
        return self.ok

    def predict_prob(self, pcm: Any) -> float | None:
        """P(turn complete) for int16 PCM bytes (or a float32 numpy array) at 16 kHz. None if the
        model isn't usable or there's too little audio. Mirrors the upstream reference preprocessing
        exactly: last 8 s, Whisper feature extractor (`do_normalize`, pad to 8 s), sigmoid output."""
        if not self.ok and not self.load():
            return None
        np = self._np
        try:
            if isinstance(pcm, (bytes, bytearray, memoryview)):
                audio = np.frombuffer(bytes(pcm), dtype=np.int16).astype(np.float32) / 32768.0
            else:
                audio = np.asarray(pcm, dtype=np.float32)
            if audio.ndim > 1:
                audio = audio.reshape(-1)
            if audio.shape[0] < _MIN_SAMPLES:
                return None
            window = _SAMPLE_RATE * _WINDOW_S
            if audio.shape[0] > window:
                audio = audio[-window:]              # keep the END of the turn
            inputs = self._feat(audio, sampling_rate=_SAMPLE_RATE, return_tensors="np",
                                padding="max_length", max_length=window, truncation=True,
                                do_normalize=True)
            feats = np.expand_dims(inputs.input_features.squeeze(0).astype(np.float32), axis=0)
            out = self._session.run(None, {"input_features": feats})
            prob = float(np.asarray(out[0]).reshape(-1)[0])
            self._errors = 0
            return prob
        except Exception as e:
            self._errors += 1
            if self._errors >= _MAX_ERRORS:
                self._ok = False
                print(f"[otto] smart-turn disabled after repeated errors ({e!r})",
                      file=sys.stderr, flush=True)
            return None

    def target_for(self, pcm: Any) -> tuple[int, str] | None:
        """The end-silence window (ms) + reason this audio warrants, or None to leave it to the
        regex endpointer / default."""
        prob = self.predict_prob(pcm)
        if prob is None:
            return None
        return prob_to_end_silence(prob, min_ms=self.min_ms, base_ms=self.base_ms,
                                   max_ms=self.max_ms, high=self.high, low=self.low)


def build_turn_model(settings: Any) -> SmartTurnModel | None:
    """Construct a SmartTurnModel from config, or None when turn_model is off / no model file is
    present (the caller then uses the regex `Endpointer`). Does NOT load weights — call `.load()`
    (off the audio read loop) to warm it."""
    path = resolve_model_path(settings)
    if path is None:
        return None
    stt = settings.voice.stt
    return SmartTurnModel(
        path,
        min_ms=int(getattr(stt, "endpoint_min_ms", 250)),
        base_ms=int(getattr(stt, "endpoint_base_ms", 500)),
        max_ms=int(getattr(stt, "endpoint_max_ms", 900)),
        high=float(getattr(stt, "turn_model_high_prob", 0.65)),
        low=float(getattr(stt, "turn_model_low_prob", 0.35)),
    )
