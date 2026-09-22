"""Speaker verification — "is this MY voice?" — the layer content can't provide.

The smart-gate (laya) decides whether an utterance is *addressed to* Orrin, but it reasons about
TEXT, so it can't tell your imperative side-talk to a colleague ("...just send an email") or a voice
on the TV from a real command — they're identical as words. Speaker verification separates them on a
different axis: WHO is speaking. You enroll your voice once (a stored "voiceprint" embedding); each
utterance is embedded and compared (cosine similarity) to it. Speech that isn't yours is dropped
regardless of content — other people, media, and Orrin's own TTS echo all fall away.

Backend: a local x-vector model via `transformers` (WavLM-SV) + torch — both already bundled, so no
new dependency and no API key. Loads once in a background thread; until it's ready (or if the model
can't load) `verify()` returns None and the caller skips the check (fail-open — never lock you out).

Audio in: float32 mono PCM in [-1, 1] at 16 kHz (what the mic/RealtimeSTT already produces).
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import numpy as np

TARGET_SR = 16000
DEFAULT_MODEL = "microsoft/wavlm-base-plus-sv"


def to_float32_mono(pcm: bytes) -> np.ndarray:
    """int16 PCM bytes → float32 mono in [-1, 1]."""
    if not pcm:
        return np.zeros(0, dtype=np.float32)
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    return a


def _resample(audio: np.ndarray, sr: int) -> np.ndarray:
    if sr == TARGET_SR or audio.size == 0:
        return audio
    try:
        import torch
        import torchaudio
        t = torch.from_numpy(audio).float().unsqueeze(0)
        out = torchaudio.functional.resample(t, sr, TARGET_SR)
        return out.squeeze(0).numpy()
    except Exception:
        # crude linear fallback so enrollment from odd-rate files still works
        n = int(round(audio.size * TARGET_SR / sr))
        if n <= 0:
            return audio
        xp = np.linspace(0, 1, audio.size, dtype=np.float32)
        x = np.linspace(0, 1, n, dtype=np.float32)
        return np.interp(x, xp, audio).astype(np.float32)


class SpeakerVerifier:
    """Lazy, thread-safe x-vector speaker verifier. Enroll once → verify each utterance."""

    def __init__(self, model_id: str = DEFAULT_MODEL,
                 voiceprint_path: str | Path | None = None) -> None:
        self._model_id = model_id or DEFAULT_MODEL
        self._extractor = None
        self._model = None
        self._ready = False
        self._failed = False
        self._lock = threading.Lock()
        self._voiceprint: np.ndarray | None = None
        self._vp_path = Path(voiceprint_path).expanduser() if voiceprint_path else None
        if self._vp_path and self._vp_path.exists():
            try:
                self._voiceprint = np.load(self._vp_path)
            except Exception as e:
                print(f"[otto] speaker: couldn't load voiceprint {self._vp_path}: {e!r}",
                      file=sys.stderr, flush=True)

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        """Load the embedding model in the background (first load downloads ~weights once)."""
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self) -> bool:
        try:
            import torch
            from transformers import AutoFeatureExtractor, WavLMForXVector
            self._extractor = AutoFeatureExtractor.from_pretrained(self._model_id)
            self._model = WavLMForXVector.from_pretrained(self._model_id)
            self._model.eval()
            self._torch = torch
            self._ready = True
            print(f"[otto] speaker verifier ready ({self._model_id})",
                  file=sys.stderr, flush=True)
            return True
        except Exception as e:
            self._failed = True
            print(f"[otto] speaker verifier unavailable ({e!r}) — skipping voice-ID",
                  file=sys.stderr, flush=True)
            return False

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def has_voiceprint(self) -> bool:
        return self._voiceprint is not None

    # -- embedding / scoring ----------------------------------------------
    def embed(self, audio: np.ndarray, sr: int = TARGET_SR) -> np.ndarray | None:
        """L2-normalized x-vector for one clip, or None if not ready / audio too short."""
        if not self._ready or self._model is None:
            return None
        audio = _resample(np.asarray(audio, dtype=np.float32), sr)
        if audio.size < TARGET_SR // 2:      # < ~0.5s — too short to identify a speaker
            return None
        try:
            with self._lock:
                inputs = self._extractor([audio], sampling_rate=TARGET_SR,
                                         return_tensors="pt", padding=True)
                with self._torch.no_grad():
                    emb = self._model(**inputs).embeddings
                emb = self._torch.nn.functional.normalize(emb, dim=-1)
                return emb.cpu().numpy()[0].astype(np.float32)
        except Exception as e:
            print(f"[otto] speaker embed failed: {e!r}", file=sys.stderr, flush=True)
            return None

    def enroll(self, clips: list[tuple[np.ndarray, int]]) -> np.ndarray | None:
        """Build a voiceprint = mean of per-clip embeddings (then normalized). Saves it if a path
        was configured. `clips` is [(audio_float32, sr), ...]."""
        embs = []
        for audio, sr in clips:
            e = self.embed(audio, sr)
            if e is not None:
                embs.append(e)
        if not embs:
            return None
        vp = np.mean(np.stack(embs), axis=0)
        vp = vp / (np.linalg.norm(vp) + 1e-9)
        self._voiceprint = vp.astype(np.float32)
        if self._vp_path is not None:
            try:
                self._vp_path.parent.mkdir(parents=True, exist_ok=True)
                np.save(self._vp_path, self._voiceprint)
            except Exception as e:
                print(f"[otto] speaker: couldn't save voiceprint: {e!r}",
                      file=sys.stderr, flush=True)
        return self._voiceprint

    @staticmethod
    def cosine(a: np.ndarray, b: np.ndarray) -> float:
        a = a / (np.linalg.norm(a) + 1e-9)
        b = b / (np.linalg.norm(b) + 1e-9)
        return float(np.dot(a, b))

    def verify(self, audio: np.ndarray, sr: int = TARGET_SR) -> float | None:
        """Cosine similarity of this clip to the enrolled voiceprint (higher = more likely you).
        None if not ready, no voiceprint, or the clip is too short — caller then skips the check."""
        if self._voiceprint is None:
            return None
        e = self.embed(audio, sr)
        if e is None:
            return None
        return self.cosine(e, self._voiceprint)
