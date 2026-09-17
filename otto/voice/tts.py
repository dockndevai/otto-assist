"""The mouth: text-to-speech with barge-in.

Three engines:

* **system** (default) — macOS ``say``: native, reliable even from a launchd background app,
  trivially interruptible. Robotic, but zero setup.
* **kokoro** — a small, high-quality *local neural* voice (Kokoro, 82M, ONNX). Fully offline,
  no API key, far more natural than ``say``. Needs the ``kokoro-onnx`` package and two model
  files (see ``otto.voice.fetch_kokoro``); if either is missing it logs how to get them and
  falls back to ``say`` so speech never silently breaks.
* **elevenlabs** — premium streaming cloud voice via RealtimeTTS (needs ``ELEVENLABS_API_KEY``).

``system`` and ``kokoro`` share one worker thread and one subprocess handle: ``say`` speaks
directly, while ``kokoro`` synthesizes each sentence to a temp WAV and plays it with ``afplay``.
Both go through the same ``_proc`` so ``stop()`` (barge-in) and ``is_playing()`` work identically.
"""

from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time

from ..config import OTTO_HOME, Settings

# A curated subset of Kokoro v1.0 voices (there are more; these read well for an assistant).
# name = the id passed to Kokoro.create(); label/locale are for the settings picker.
KOKORO_VOICES = [
    {"name": "af_heart", "locale": "en_US", "sample": "Warm US English (female)"},
    {"name": "af_bella", "locale": "en_US", "sample": "Bright US English (female)"},
    {"name": "af_nicole", "locale": "en_US", "sample": "Soft US English (female)"},
    {"name": "am_michael", "locale": "en_US", "sample": "US English (male)"},
    {"name": "am_puck", "locale": "en_US", "sample": "Lively US English (male)"},
    {"name": "bf_emma", "locale": "en_GB", "sample": "British English (female)"},
    {"name": "bm_george", "locale": "en_GB", "sample": "British English (male)"},
]


def list_system_voices() -> list[dict[str, str]]:
    """Installed macOS `say` voices as [{'name','locale','sample'}], English first.

    Parsed from `say -v '?'`, whose lines look like:
        Samantha            en_US    # Hello, my name is Samantha.
    """
    try:
        out = subprocess.run(["/usr/bin/say", "-v", "?"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    voices: list[dict[str, str]] = []
    for line in out.splitlines():
        m = re.match(r"^(.+?)\s+([a-z]{2}[_-][A-Z]{2}\S*)\s+#\s*(.*)$", line)
        if not m:
            continue
        voices.append({"name": m.group(1).strip(),
                       "locale": m.group(2).replace("-", "_"),
                       "sample": m.group(3).strip()})
    # English voices first (most relevant), then the rest; alphabetical within each.
    voices.sort(key=lambda v: (not v["locale"].startswith("en"), v["name"].lower()))
    return voices


def list_kokoro_voices() -> list[dict[str, str]]:
    """The Kokoro voices offered in the picker (static — no model load required)."""
    return list(KOKORO_VOICES)


def kokoro_model_dir(settings: Settings) -> "os.PathLike[str] | str":
    """Where the Kokoro model files live (config override, else OTTO_HOME/models/kokoro)."""
    d = getattr(settings.voice.tts, "kokoro_model_dir", "") or ""
    from pathlib import Path
    return Path(d).expanduser() if d else (OTTO_HOME / "models" / "kokoro")


class Mouth:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._engine = settings.voice.tts.engine
        self._voice = settings.voice.tts.voice
        # #i18n Tier 1: Kokoro's voices are English and mangle Hindi/Hinglish words. When a
        # non-English language is set and we have a mapped macOS `say` voice for it, speak with
        # that voice instead (Rishi = Indian English, Lekha = Hindi). English is unaffected.
        vlang = (getattr(settings.voice, "language", "en") or "en").lower()
        lang_voices = getattr(settings.voice.tts, "lang_voices", {}) or {}
        if vlang not in ("en", "") and vlang in {k.lower(): 1 for k in lang_voices}:
            say_voice = {k.lower(): v for k, v in lang_voices.items()}[vlang]
            self._engine = "system"
            self._voice = say_voice
        # macOS say / kokoro shared path
        self._q: "queue.Queue[str | None]" = queue.Queue()
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._last_end = 0.0   # when the last utterance finished (for echo suppression)
        # kokoro (local neural)
        self._kokoro = None
        self._kokoro_voice = (getattr(settings.voice.tts, "kokoro_voice", "") or "af_heart")
        self._kokoro_speed = float(getattr(settings.voice.tts, "kokoro_speed", 1.0) or 1.0)
        # ElevenLabs (RealtimeTTS) path
        self._stream = None

    def start(self) -> None:
        if self._engine == "elevenlabs":
            self._start_elevenlabs()
            return
        if self._engine == "kokoro" and not self._init_kokoro():
            # Model/lib missing → don't go mute; fall back to the always-available system voice.
            self._engine = "system"
        self._worker = threading.Thread(target=self._speak_loop, daemon=True)
        self._worker.start()

    # -- kokoro (local neural voice) --------------------------------------
    def _init_kokoro(self) -> bool:
        """Load the Kokoro ONNX model once. Returns False (with a helpful log) if the package
        or the model files aren't available, so the caller can fall back to `say`."""
        from pathlib import Path
        try:
            from kokoro_onnx import Kokoro
        except Exception:
            print("[otto] kokoro voice needs the 'kokoro-onnx' package "
                  "(pip install kokoro-onnx). Falling back to the system voice.",
                  file=sys.stderr, flush=True)
            return False
        mdir = Path(kokoro_model_dir(self.settings))
        model = mdir / "kokoro-v1.0.onnx"
        voices = mdir / "voices-v1.0.bin"
        if not (model.exists() and voices.exists()):
            print(f"[otto] kokoro model files not found in {mdir}. Run "
                  "`python -m otto.voice.fetch_kokoro` to download them. "
                  "Falling back to the system voice.", file=sys.stderr, flush=True)
            return False
        try:
            self._kokoro = Kokoro(str(model), str(voices))
            print(f"[otto] kokoro voice loaded ({self._kokoro_voice}) from {mdir}",
                  file=sys.stderr, flush=True)
            return True
        except Exception as e:
            print(f"[otto] kokoro failed to load: {e!r}. Falling back to the system voice.",
                  file=sys.stderr, flush=True)
            self._kokoro = None
            return False

    def _synth_kokoro(self, text: str, voice: str | None = None) -> str | None:
        """Synthesize `text` to a temp WAV with Kokoro; return the path (caller deletes)."""
        if self._kokoro is None:
            return None
        try:
            import soundfile as sf
            samples, sr = self._kokoro.create(
                text, voice=(voice or self._kokoro_voice), speed=self._kokoro_speed,
                lang="en-us")
            fd, path = tempfile.mkstemp(prefix="orrin-tts-", suffix=".wav")
            os.close(fd)
            sf.write(path, samples, sr)
            return path
        except Exception as e:
            print(f"[otto] kokoro synth failed: {e!r} (using system voice for this line)",
                  file=sys.stderr, flush=True)
            return None

    # -- say / kokoro worker ----------------------------------------------
    def _render_job(self, text: str) -> tuple[list[str], str | None]:
        """Build the playback argv for one utterance + an optional temp file to clean up.
        kokoro → synth to WAV + afplay; system (or kokoro synth failure) → `say`."""
        if self._engine == "kokoro" and self._kokoro is not None:
            wav = self._synth_kokoro(text)
            if wav:
                return ["/usr/bin/afplay", wav], wav
        argv = ["/usr/bin/say"]
        if self._voice:
            argv += ["-v", self._voice]
        argv.append(text)
        return argv, None

    def _speak_loop(self) -> None:
        while True:
            text = self._q.get()
            if text is None:
                return
            argv, tmp = self._render_job(text)
            try:
                with self._lock:
                    self._proc = subprocess.Popen(argv)
                rc = self._proc.wait()
                print(f"[otto] tts rc={rc} via {argv[0].split('/')[-1]} text={text[:40]!r}",
                      file=sys.stderr, flush=True)
            except Exception as e:
                print(f"[otto] tts FAILED: {e!r}", file=sys.stderr, flush=True)
            finally:
                with self._lock:
                    self._proc = None
                if tmp:
                    try:
                        os.unlink(tmp)
                    except Exception:
                        pass
                if self._q.empty():
                    self._last_end = time.monotonic()

    def set_voice(self, voice: str) -> None:
        """Change the voice live (applies to the next utterance). Interpreted per engine:
        a macOS `say` voice name for `system`, or a Kokoro voice id for `kokoro`."""
        v = (voice or "").strip()
        if self._engine == "kokoro":
            if v:
                self._kokoro_voice = v
        else:
            self._voice = v

    def preview(self, text: str, voice: str) -> None:
        """Speak a one-off sample in `voice` without changing the configured voice."""
        if self._engine == "elevenlabs":
            return
        if self._engine == "kokoro" and self._kokoro is not None:
            wav = self._synth_kokoro(text, voice=voice or self._kokoro_voice)
            if wav:
                try:
                    subprocess.Popen(["/usr/bin/afplay", wav])
                except Exception:
                    pass
                return
        argv = ["/usr/bin/say"]
        if voice:
            argv += ["-v", voice]
        argv.append(text)
        try:
            subprocess.Popen(argv)
        except Exception:
            pass

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
        from RealtimeTTS import ElevenlabsEngine, TextToAudioStream

        kwargs = {"api_key": os.environ.get("ELEVENLABS_API_KEY", "")}
        if self._voice:
            kwargs["voice"] = self._voice
        self._stream = TextToAudioStream(ElevenlabsEngine(**kwargs))
