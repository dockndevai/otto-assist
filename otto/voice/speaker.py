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


def _safe_name(name: str) -> str:
    out = "".join(c for c in (name or "").strip().lower() if c.isalnum() or c in " -_")
    return "-".join(out.split())[:40] or ""


class SpeakerVerifier:
    """Lazy, thread-safe x-vector speaker verifier with MULTIPLE named voiceprints (for a shared /
    family machine). Enroll each person once (name + a few clips); each utterance is matched to the
    closest enrolled voice, and accepted only if that best match is above threshold AND in the
    active set. Voiceprints live as <name>.npy under a directory."""

    def __init__(self, model_id: str = DEFAULT_MODEL,
                 voiceprints_dir: str | Path | None = None) -> None:
        self._model_id = model_id or DEFAULT_MODEL
        self._extractor = None
        self._model = None
        self._ready = False
        self._failed = False
        self._lock = threading.Lock()
        self._prints: dict[str, np.ndarray] = {}   # name -> L2-normalized voiceprint
        self._dir = Path(voiceprints_dir).expanduser() if voiceprints_dir else None
        self._load_prints()

    def _load_prints(self) -> None:
        self._prints = {}
        if not self._dir:
            return
        try:
            # Migrate a legacy single voiceprint.npy (sibling of the dir) → "me".
            legacy = self._dir.parent / "voiceprint.npy"
            if legacy.exists() and not (self._dir / "me.npy").exists():
                self._dir.mkdir(parents=True, exist_ok=True)
                import shutil
                shutil.copy(str(legacy), str(self._dir / "me.npy"))
            if self._dir.exists():
                for f in sorted(self._dir.glob("*.npy")):
                    try:
                        self._prints[f.stem] = np.load(f)
                    except Exception:
                        pass
        except Exception as e:
            print(f"[otto] speaker: couldn't load voiceprints: {e!r}", file=sys.stderr, flush=True)

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
        return bool(self._prints)

    @property
    def names(self) -> list[str]:
        return list(self._prints)

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

    def enroll(self, clips: list[tuple[np.ndarray, int]], name: str = "me") -> str | None:
        """Build a voiceprint (mean of per-clip embeddings, normalized) for `name` and save it as
        <name>.npy. `clips` is [(audio_float32, sr), ...]. Returns the saved name, or None."""
        embs = []
        for audio, sr in clips:
            e = self.embed(audio, sr)
            if e is not None:
                embs.append(e)
        if not embs:
            return None
        vp = np.mean(np.stack(embs), axis=0)
        vp = vp / (np.linalg.norm(vp) + 1e-9)
        name = _safe_name(name) or "me"
        self._prints[name] = vp.astype(np.float32)
        if self._dir is not None:
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
                np.save(self._dir / f"{name}.npy", self._prints[name])
            except Exception as e:
                print(f"[otto] speaker: couldn't save voiceprint: {e!r}",
                      file=sys.stderr, flush=True)
        return name

    @staticmethod
    def cosine(a: np.ndarray, b: np.ndarray) -> float:
        a = a / (np.linalg.norm(a) + 1e-9)
        b = b / (np.linalg.norm(b) + 1e-9)
        return float(np.dot(a, b))

    def match(self, audio: np.ndarray, sr: int = TARGET_SR) -> tuple[str, float] | None:
        """Best-matching enrolled voice for this clip: (name, cosine). None if not ready, no
        voiceprints, or the clip is too short — caller then skips the check (fail-open)."""
        if not self._prints:
            return None
        e = self.embed(audio, sr)
        if e is None:
            return None
        best, best_s = None, -2.0
        for name, vp in self._prints.items():
            s = self.cosine(e, vp)
            if s > best_s:
                best, best_s = name, s
        return (best, best_s) if best is not None else None
