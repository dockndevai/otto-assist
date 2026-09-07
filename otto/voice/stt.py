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


def _pretrust_silero_vad() -> None:
    """Stop RealtimeSTT's Silero-VAD init from hanging in a non-interactive process.

    RealtimeSTT loads the VAD via ``torch.hub.load('snakers4/silero-vad', ...)``. When the
    repo isn't on torch.hub's trusted list, torch asks ``Do you trust this repository? (y/N)``
    on stdin — which instantly raises ``EOFError`` under launchd / a service / any headless
    run, so the recorder never starts and Otto silently can't hear you. Marking the repo
    trusted up front (equivalent to ``trust_repo=True``) skips that prompt.
    """
    try:
        import torch.hub as _hub
    except Exception:
        return  # torch-free build: VAD comes from the pip silero package, no prompt
    try:
        if getattr(_hub._check_repo_is_trusted, "_otto_trusted", False):
            return
        _orig = _hub._check_repo_is_trusted

        def _check(repo_owner, repo_name, *a, **k):
            if "silero-vad" in f"{repo_owner}/{repo_name}":
                return                      # trust Silero VAD without prompting
            return _orig(repo_owner, repo_name, *a, **k)

        _check._otto_trusted = True         # type: ignore[attr-defined]
        _hub._check_repo_is_trusted = _check
    except Exception:
        pass


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
        on_realtime: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self._loop = loop
        self._on_final = on_final
        self._on_partial = on_partial
        self._on_speech_start = on_speech_start
        self._on_wake = on_wake
        self._on_recording_stop = on_recording_stop
        self._on_realtime = on_realtime
        self._recorder = None
        self._thread: threading.Thread | None = None
        self._running = False

    def _post(self, fn, *args) -> None:
        """Bounce a callback from the recorder thread onto the asyncio loop."""
        self._loop.call_soon_threadsafe(fn, *args)

    def start(self) -> None:
        _pretrust_silero_vad()
        from RealtimeSTT import AudioToTextRecorder  # lazy import

        cfg = self.settings.voice.stt
        params = dict(
            model=cfg.model,
            language=cfg.language,
            spinner=False,
            level=logging.WARNING,           # quiet RealtimeSTT's very chatty logging
            # Latency: this Mac has no CUDA and Whisper float16 isn't supported on CPU (it
            # silently upcasts to slow float32), so run int8 on CPU with a greedy beam — far
            # faster transcription with negligible accuracy loss for short spoken commands.
            device="cpu",
            compute_type="int8",
            beam_size=1,
            beam_size_realtime=1,
            # Wait after you stop before ending the turn (lower = snappier, higher = won't
            # clip a mid-thought pause).
            post_speech_silence_duration=cfg.silence_seconds,
            # VAD gating: trigger on clearer speech and ignore short noise blips, so the
            # recorder hands Whisper real speech instead of ambient sound it hallucinates over.
            silero_sensitivity=cfg.silero_sensitivity,
            min_length_of_recording=cfg.min_length_of_recording,
            # Leave silero_use_onnx UNSET. It is RealtimeSTT's legacy switch and both True
            # and False resolve to SILERO_BACKEND_LEGACY, which loads the VAD over
            # torch.hub — a GitHub download on first run that needs a CA store and an
            # interactive trust prompt, so it fails outright in a bundled app. Unset means
            # silero_backend="auto", which uses the .onnx weights shipped inside the
            # silero_vad package via onnxruntime: offline, no download, no prompt.
            on_recording_start=lambda: self._post(self._on_speech_start),
        )
        # Prime Whisper with the wake word so it actually transcribes the name (small
        # models otherwise drop "Otto" -> hear "auto"/"your"). Big reliability win for the
        # software ("phrase") wake word.
        if cfg.wakeword_backend == "phrase" and cfg.wake_words:
            name = cfg.wake_words.strip().title()          # "Hey Otto"
            params["initial_prompt"] = f"{name}. {name.split()[-1]}, "
        if self._on_recording_stop is not None:
            params["on_recording_stop"] = lambda: self._post(self._on_recording_stop)

        # Live captions: stream partial words as you speak, so the dashboard visibly reacts to
        # your voice (transcribed by a small fast model). Optional; off saves CPU on always-on.
        if cfg.realtime_partials and self._on_realtime is not None:
            params["enable_realtime_transcription"] = True
            params["realtime_model_type"] = cfg.realtime_model or "tiny.en"
            params["realtime_processing_pause"] = 0.15
            params["on_realtime_transcription_update"] = \
                lambda t: self._post(self._on_realtime, t)
        else:
            params["enable_realtime_transcription"] = False

        # "phrase" / "efficientword" transcribe everything (continuous VAD); the wake is
        # decided elsewhere (phrase match, or the EfficientWord-Net listener). Other
        # backends use RealtimeSTT's own hardware wake-word gate.
        use_gate = bool(cfg.wake_words) and cfg.wakeword_backend not in ("phrase", "efficientword")
        if use_gate:
            params.update(
                wakeword_backend=cfg.wakeword_backend,
                wake_words=cfg.wake_words,
                wake_words_sensitivity=cfg.wake_word_sensitivity,
                on_wakeword_detected=lambda: self._post(self._on_wake),
            )
            if cfg.wakeword_backend in ("pvporcupine", "porcupine", "pvp"):
                _install_porcupine_custom_keyword_shim()
            elif cfg.wakeword_backend in ("oww", "openwakeword") and cfg.oww_model_path:
                # Custom trained openWakeWord model (e.g. "hey otto"): load it directly and
                # match on its own name so only this model triggers.
                from pathlib import Path
                path = os.path.expanduser(cfg.oww_model_path)
                params["openwakeword_model_paths"] = path
                params["openwakeword_inference_framework"] = cfg.oww_inference_framework
                params["wake_words"] = Path(path).stem

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

    def set_mic(self, on: bool) -> None:
        """Enable/disable microphone capture at the recorder. Used for strict half-duplex:
        turn the mic OFF while Otto is speaking so it never captures (and transcribes) its own
        voice through the speaker — the reliable cure for the self-echo loop."""
        if self._recorder is not None:
            try:
                self._recorder.set_microphone(on)
            except Exception:
                pass

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
