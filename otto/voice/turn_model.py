"""Smart Turn v3 — a learned, LOCAL end-of-turn detector (the real analogue of semantic VAD).

The regex `Endpointer` in `endpointer.py` *guesses* whether you've finished from the partial text
(terminal punctuation, a trailing "and", a hold phrase). This module replaces that guess with the
same CLASS of component OpenAI's `semantic_vad` uses: a tiny model that looks at the user's **audio
so far** and returns P(the turn is complete). We use Pipecat's Smart Turn v3 — a Whisper-Tiny encoder
+ a shallow classifier head, ~8 MB INT8 ONNX, BSD-2 licensed — which runs on the onnxruntime we
already ship for Silero VAD.

Audio in, probability out. No network, no cloud, no engine round-trip — and no heavy dependency: the
Whisper feature preprocessing (log-mel of the last 8 s) is done in numpy here, using the mel
filterbank + window arrays bundled in `smart_turn_fe.npz`. Those arrays were lifted verbatim from
`transformers.WhisperFeatureExtractor(chunk_length=8)` and the numpy path is byte-for-byte verified
against it (features allclose 1e-4, identical model probabilities), so we match the model's training
preprocessing exactly without pulling transformers into the packaged app.

`prob_to_end_silence` maps the probability onto the same min/base/max end-silence windows the regex
endpointer uses, so the native helper's VAD is driven identically either way.

Fail-safe by construction: if onnxruntime, the bundled FE arrays, or the model file is missing — or
inference errors repeatedly — `load()`/`target_for()` report unavailable and the caller falls back to
the regex `Endpointer`. Nothing here ever raises into the audio path.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from ..config import OTTO_HOME

_SAMPLE_RATE = 16000
_WINDOW_S = 8
_WINDOW = _SAMPLE_RATE * _WINDOW_S        # 8 s of samples (the model's input length)
_MIN_SAMPLES = _SAMPLE_RATE // 4          # need ~0.25 s of audio before a prediction means anything
_MAX_ERRORS = 3                           # disable the model after this many consecutive failures
_FE_DATA = Path(__file__).with_name("smart_turn_fe.npz")   # bundled mel filterbank + window


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
        self._np: Any = None
        self._mel: Any = None            # (201, 80) mel filterbank
        self._window: Any = None         # (400,) hann window, float64
        self._n_fft = 400
        self._hop = 160
        self._loaded = False             # load() has run (ok or not)
        self._ok = False                 # session + FE arrays are usable
        self._errors = 0

    @property
    def ok(self) -> bool:
        return self._ok and self._errors < _MAX_ERRORS

    def load(self) -> bool:
        """Import numpy/onnxruntime, build the ONNX session, and load the bundled FE arrays.
        Idempotent; never raises. Returns True iff the model is ready to predict."""
        if self._loaded:
            return self.ok
        self._loaded = True
        try:
            import numpy as np
            import onnxruntime as ort

            if not self.model_path.exists():
                raise FileNotFoundError(self.model_path)
            fe = np.load(_FE_DATA)
            self._mel = fe["mel_filters"].astype(np.float32)
            self._window = fe["window"].astype(np.float64)
            self._n_fft = int(fe["n_fft"])
            self._hop = int(fe["hop_length"])
            so = ort.SessionOptions()
            so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            so.inter_op_num_threads = 1
            so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            self._session = ort.InferenceSession(str(self.model_path), sess_options=so)
            self._np = np
            self._ok = True
        except Exception as e:                       # missing dep / bad file / unsupported op
            print(f"[otto] smart-turn model unavailable ({e!r}); using regex endpointer",
                  file=sys.stderr, flush=True)
            self._ok = False
        return self.ok

    def _features(self, audio: Any) -> Any:
        """Whisper log-mel of the last 8 s, in pure numpy. Matches WhisperFeatureExtractor
        (chunk_length=8, do_normalize) exactly — verified byte-for-byte against it."""
        np = self._np
        a = np.asarray(audio, dtype=np.float32)
        if a.shape[0] > _WINDOW:
            a = a[-_WINDOW:]
        real = a.shape[0]
        a = np.pad(a, (0, _WINDOW - real)).astype(np.float64)
        # do_normalize: zero-mean unit-var over the REAL samples, then zero the padding.
        m, v = a[:real].mean(), a[:real].var()
        a = (a - m) / np.sqrt(v + 1e-7)
        a[real:] = 0.0
        # center-reflect pad, frame, window, rFFT, power, mel, log10 — the Whisper spectrogram path.
        pad = self._n_fft // 2
        ap = np.pad(a, (pad, pad), mode="reflect")
        nfr = 1 + (ap.size - self._n_fft) // self._hop
        frames = np.lib.stride_tricks.as_strided(
            ap, shape=(nfr, self._n_fft), strides=(ap.strides[0] * self._hop, ap.strides[0])).copy()
        frames = frames * self._window
        spec = np.fft.rfft(frames, n=self._n_fft, axis=1)
        mag = (np.abs(spec).astype(np.float64) ** 2).T
        melspec = np.maximum(1e-10, self._mel.T @ mag)
        log = np.log10(melspec)[:, :-1]
        log = np.maximum(log, log.max() - 8.0)
        log = (log + 4.0) / 4.0
        return log.astype(np.float32)

    def predict_prob(self, pcm: Any) -> float | None:
        """P(turn complete) for int16 PCM bytes (or a float32 numpy array) at 16 kHz. None if the
        model isn't usable or there's too little audio."""
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
            feats = np.expand_dims(self._features(audio), axis=0)
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
