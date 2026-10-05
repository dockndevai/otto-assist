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
import sys
import threading
import time
from typing import Callable

from ..config import Settings


def _resolve_lang(voice, stt):
    """Map voice.language → (whisper model, whisper language). English keeps the fast small.en;
    non-English switches to the multilingual model (#i18n Tier 1)."""
    v = (getattr(voice, "language", "en") or "en").lower()
    ml = getattr(stt, "multilingual_model", "small")
    if v in ("en", ""):
        return stt.model, stt.language            # unchanged default: small.en / en
    if v == "en-in":
        return stt.model, "en"                    # Indian English: small.en + Indian priming
    if v == "hinglish":
        # PIN to English on the multilingual model. Auto-detect (None) is disastrous on short
        # CPU utterances — it flips English/Hindi speech to Japanese/German/gibberish. Pinned
        # "en" transcribes accented English cleanly and romanizes Hindi words (Latin script),
        # which Claude reads fine and the neural voice can speak. Commands work again.
        return ml, "en"
    if v == "auto":
        return ml, None                           # true auto-detect (unreliable on CPU; advanced)
    return ml, v                                  # "hi", "ta", … a Whisper language code


def pick_mlx_model(stt, voice_language) -> str:
    """Choose the MLX Whisper model from the user's configured language (NOT the resolved whisper
    code — Hinglish resolves to whisper-lang "en" but still needs the multilingual model). English
    family → the small english model; everything else (hinglish/hi/ta/auto) → the multilingual turbo."""
    vl = (voice_language or "en").lower()
    is_en = vl in ("en", "", "en-in")
    return stt.mlx_model if is_en else stt.mlx_multilingual_model


class _PipeErrorWatch(logging.Handler):
    """Watches the root logger for RealtimeSTT's dead-pipe errors ("Error receiving data from
    connection" / EOFError). When the recorder's worker dies the parent floods these; counting
    them lets the Ears watchdog notice and rebuild even when no exception reaches our own loop."""
    def __init__(self, on_error: Callable[[], None]) -> None:
        super().__init__(level=logging.ERROR)
        self._on_error = on_error

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            return
        if "receiving data from connection" in msg or "EOFError" in msg:
            try:
                self._on_error()
            except Exception:
                pass


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
        on_health: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self._loop = loop
        self._on_final = on_final
        self._on_partial = on_partial
        self._on_speech_start = on_speech_start
        self._on_wake = on_wake
        self._on_recording_stop = on_recording_stop
        self._on_realtime = on_realtime
        self._on_health = on_health          # "deaf" when the mic dies, "ok" when it recovers
        self._consec_errors = 0
        self._deaf_reported = False
        self._recorder = None
        self._thread: threading.Thread | None = None
        self._running = False
        # Watchdog: a single recording must not grow unbounded. Without a pause the VAD never
        # ends the capture (e.g. a video/podcast playing on the speakers into the mic), so cap
        # it and force-finalize. Keeps Otto responsive to back-to-back asks instead of stuck
        # accumulating one endless utterance.
        self._max_record_s = float(
            getattr(settings.voice.stt, "max_recording_seconds", 0) or 15.0)
        self._watchdog: threading.Timer | None = None
        # Transcription engine. "mlx" records via RealtimeSTT but transcribes the captured audio with
        # mlx-whisper (Apple-Silicon GPU) instead of faster-whisper. Resolved to False in start() if
        # mlx-whisper can't be imported, so we always fall back to faster-whisper rather than go mute.
        self._use_mlx = getattr(settings.voice.stt, "transcriber", "faster-whisper") == "mlx"
        self._mlx = None                          # the mlx_whisper module, set in start()
        self._mlx_model = ""                      # resolved (english vs multilingual) in start()
        # Audio tap: accumulate the current utterance's raw PCM (via on_recorded_chunk, between
        # recording-start and -stop). Needed for speaker verification AND for the MLX transcriber
        # (which transcribes this buffer). Off = zero overhead (no chunk callback wired).
        self._capture_audio = bool(
            getattr(settings.voice.stt, "speaker_verify", False) or self._use_mlx)
        self.sample_rate = 16000
        self.last_audio: bytes | None = None      # PCM of the most recent finalized utterance
        self._utt_chunks: list[bytes] = []
        self._recording = False

    def _post(self, fn, *args) -> None:
        """Bounce a callback from the recorder thread onto the asyncio loop."""
        self._loop.call_soon_threadsafe(fn, *args)

    # -- recording watchdog (force-finalize an over-long capture) --------------
    def _on_chunk(self, chunk: bytes) -> None:
        """RealtimeSTT hands us each mic chunk. While a recording is active, buffer it so the
        finalized utterance's audio is available for speaker verification. Best-effort, cheap."""
        if self._recording and chunk:
            self._utt_chunks.append(bytes(chunk))

    def _rec_started(self) -> None:
        """RealtimeSTT began a recording. Notify the orchestrator and arm the max-duration
        watchdog so a never-ending capture (continuous ambient/media audio) still finalizes."""
        if self._capture_audio:
            self._recording = True
            self._utt_chunks = []
        self._post(self._on_speech_start)
        self._cancel_watchdog()
        if self._max_record_s > 0:
            self._watchdog = threading.Timer(self._max_record_s, self._force_finalize)
            self._watchdog.daemon = True
            self._watchdog.start()

    def _rec_stopped(self) -> None:
        """Recording ended (naturally or forced) — disarm the watchdog and notify."""
        if self._capture_audio and self._recording:
            self._recording = False
            self.last_audio = b"".join(self._utt_chunks) if self._utt_chunks else None
        self._cancel_watchdog()
        self._last_record_stop = time.monotonic()   # transcript should follow shortly
        if self._on_recording_stop is not None:
            self._post(self._on_recording_stop)

    def _cancel_watchdog(self) -> None:
        if self._watchdog is not None:
            try:
                self._watchdog.cancel()
            except Exception:
                pass
            self._watchdog = None

    def _force_finalize(self) -> None:
        """The current recording ran past the cap without a natural pause — stop it so it gets
        transcribed now (the buried-wake matcher recovers a real command from the blob; pure
        ambient just fails the wake match and is ignored). Keeps back-to-back asks working."""
        import logging
        logging.getLogger("otto").info(
            "recording exceeded %.0fs — force-finalizing", self._max_record_s)
        if self._recorder is not None:
            try:
                self._recorder.stop()
            except Exception:
                pass

    def start(self) -> None:
        _pretrust_silero_vad()
        from RealtimeSTT import AudioToTextRecorder  # lazy import

        cfg = self.settings.voice.stt
        model, lang = _resolve_lang(self.settings.voice, cfg)
        params = dict(
            model=model,
            language=lang,
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
            # Latency: kick off the final transcription DURING the end-of-speech silence gap
            # rather than after it, so the text is ready ~the moment the turn ends instead of
            # adding transcription time on top of the silence wait.
            early_transcription_on_silence=int(max(0.0, cfg.silence_seconds - 0.3) * 1000),
            # VAD gating: trigger on clearer speech and ignore short noise blips, so the
            # recorder hands Whisper real speech instead of ambient sound it hallucinates over.
            silero_sensitivity=cfg.silero_sensitivity,
            min_length_of_recording=cfg.min_length_of_recording,
            # #57: drop non-speech (media bleed) before Whisper transcribes it, and optionally
            # use silero for end-of-speech so a continuous stream still ends the turn cleanly.
            faster_whisper_vad_filter=cfg.vad_filter,
            silero_deactivity_detection=cfg.silero_deactivity_detection,
            # Leave silero_use_onnx UNSET. It is RealtimeSTT's legacy switch and both True
            # and False resolve to SILERO_BACKEND_LEGACY, which loads the VAD over
            # torch.hub — a GitHub download on first run that needs a CA store and an
            # interactive trust prompt, so it fails outright in a bundled app. Unset means
            # silero_backend="auto", which uses the .onnx weights shipped inside the
            # silero_vad package via onnxruntime: offline, no download, no prompt.
            on_recording_start=self._rec_started,
        )
        # Effective wake-word backend, with a graceful fallback: a hardware gate (pvporcupine/oww)
        # only transcribes AFTER the wake word (big CPU win), but it needs its model assets. If a
        # pvporcupine .ppn keyword file or PICOVOICE_ACCESS_KEY is missing, fall back to the
        # always-listen 'phrase' wake word so Otto keeps hearing you instead of failing to start.
        wb, ww = cfg.wakeword_backend, cfg.wake_words
        if wb in ("pvporcupine", "porcupine", "pvp"):
            ppn_ok = str(ww).endswith(".ppn") and os.path.exists(os.path.expanduser(str(ww)))
            key_ok = bool(os.environ.get("PICOVOICE_ACCESS_KEY", "").strip())
            if not (ppn_ok and key_ok):
                miss = ([] if ppn_ok else ["a .ppn keyword file"]) + \
                       ([] if key_ok else ["PICOVOICE_ACCESS_KEY"])
                print(f"[otto] pvporcupine wake word needs {' + '.join(miss)}; falling back to the "
                      "'phrase' wake word ('otto') until set.", file=sys.stderr, flush=True)
                wb, ww = "phrase", "otto"

        # Prime Whisper with the wake word so it actually transcribes the name (small
        # models otherwise drop "Otto" -> hear "auto"/"your"). Big reliability win for the
        # software ("phrase") wake word.
        prompt = ""
        if wb == "phrase" and ww:
            name = ww.strip().title()          # "Hey Otto"
            prompt = f"{name}. {name.split()[-1]}, "
        # Prime the decoder for Indian English / Hinglish so accented speech and common
        # code-switched words transcribe cleanly (#i18n Tier 1).
        vlang = (self.settings.voice.language or "en").lower()
        if vlang in ("en-in", "hinglish", "hi", "auto") or lang != "en":
            prompt += ("Indian English. Common words: theek hai, accha, haan, nahi, kya, kaise, "
                       "karo, bolo, dikhao, kholo, band karo, ruko, chalao, ek minute. ")
        # Prime app/tool proper nouns the small model otherwise mangles (e.g. "IntelliJ" ->
        # "intelligence") so "open IntelliJ" transcribes correctly.
        prompt += ("Apps: IntelliJ, PyCharm, WebStorm, VS Code, Xcode, Terminal, iTerm, Chrome, "
                   "Safari, Slack, Notion, Obsidian, Spotify, Finder, GitHub, Claude, Orrin. ")
        if prompt:
            params["initial_prompt"] = prompt
        params["on_recording_stop"] = self._rec_stopped
        if self._capture_audio:                     # tap raw PCM for speaker verification
            params["on_recorded_chunk"] = self._on_chunk

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
        use_gate = bool(ww) and wb not in ("phrase", "efficientword")
        if use_gate:
            params.update(
                wakeword_backend=wb,
                wake_words=ww,
                wake_words_sensitivity=cfg.wake_word_sensitivity,
                on_wakeword_detected=lambda: self._post(self._on_wake),
            )
            if wb in ("pvporcupine", "porcupine", "pvp"):
                _install_porcupine_custom_keyword_shim()
            elif wb in ("oww", "openwakeword") and cfg.oww_model_path:
                # Custom trained openWakeWord model (e.g. "hey otto"): load it directly and
                # match on its own name so only this model triggers.
                from pathlib import Path
                path = os.path.expanduser(cfg.oww_model_path)
                params["openwakeword_model_paths"] = path
                params["openwakeword_inference_framework"] = cfg.oww_inference_framework
                params["wake_words"] = Path(path).stem

        self._params = params            # kept so the watchdog can rebuild after a crash
        self._recorder = AudioToTextRecorder(**params)
        self._running = True
        self._last_rebuild = 0.0
        self._started_at = time.monotonic()   # for a startup grace before flagging "never spawned"
        self._unhealthy_streak = 0            # consecutive unhealthy checks → alert if unrecoverable
        if self._use_mlx:
            self._init_mlx(lang)         # sets self._mlx / self._use_mlx (falls back on failure)
        # Stall detection: if a recording finishes but no transcript comes back, the
        # transcription pipe is wedged (worker alive but its parent pipe broke — the EOFError
        # flood). Track the last recording-stop and the last text so the watchdog can catch it.
        self._last_record_stop = 0.0
        self._last_text = 0.0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # Watchdog: RealtimeSTT runs the recorder in child processes; if a worker dies the
        # parent silently spins on a dead pipe (EOFError flood) and Otto goes deaf. Detect that
        # (dead worker OR the EOF flood) and rebuild so speech keeps working without a restart.
        self._pipe_errors = 0
        try:
            logging.getLogger().addHandler(_PipeErrorWatch(self._note_pipe_error))
        except Exception:
            pass
        self._health = threading.Thread(target=self._health_loop, daemon=True)
        self._health.start()

    def _note_pipe_error(self) -> None:
        self._pipe_errors += 1

    def _init_mlx(self, lang) -> None:
        """Import mlx-whisper and pick the model (English vs multilingual). On any failure, log and
        fall back to faster-whisper so speech never breaks."""
        cfg = self.settings.voice.stt
        try:
            import mlx_whisper
            self._mlx = mlx_whisper
            # Model by CONFIGURED language (hinglish → multilingual turbo even though its whisper
            # code is pinned "en"); transcribe language is the resolved whisper code.
            self._mlx_model = pick_mlx_model(cfg, getattr(self.settings.voice, "language", "en"))
            self._mlx_lang = None if (not lang or lang == "auto") else lang
            self._mlx_fails = 0
            print(f"[otto] MLX transcriber enabled ({self._mlx_model}, lang={self._mlx_lang})",
                  file=sys.stderr, flush=True)
            # First transcription loads weights + compiles Metal kernels (~15-20s); warm it in the
            # background so the user's first real command isn't slow.
            def _warm():
                try:
                    import numpy as np
                    self._mlx.transcribe(np.zeros(16000, dtype=np.float32),
                                         path_or_hf_repo=self._mlx_model)
                    print("[otto] MLX transcriber warm", file=sys.stderr, flush=True)
                except Exception as e:
                    print(f"[otto] MLX warmup failed: {e!r}", file=sys.stderr, flush=True)
            threading.Thread(target=_warm, daemon=True).start()
        except Exception as e:
            self._use_mlx = False
            self._mlx = None
            print(f"[otto] mlx-whisper unavailable ({e!r}) — using faster-whisper",
                  file=sys.stderr, flush=True)

    def _transcribe_mlx(self, pcm: bytes | None) -> str:
        """Transcribe the just-recorded utterance PCM with mlx-whisper (Apple-Silicon GPU)."""
        if not pcm or self._mlx is None:
            return ""
        import numpy as np
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if audio.size < 1600:                       # < ~0.1s — nothing worth transcribing
            return ""
        kw = {"path_or_hf_repo": self._mlx_model}
        if self._mlx_lang:
            kw["language"] = self._mlx_lang
        text = (self._mlx.transcribe(audio, **kw).get("text") or "").strip()
        # Release MLX's Metal buffer cache so it doesn't accumulate across utterances.
        try:
            import mlx.core as mx
            (getattr(mx, "clear_cache", None) or getattr(mx.metal, "clear_cache", lambda: None))()
        except Exception:
            pass
        return text

    def _run(self) -> None:
        while self._running:
            try:
                if self._use_mlx:
                    # RealtimeSTT still does (wake ->) record + VAD; we transcribe the captured
                    # PCM with MLX instead of faster-whisper. On a transient MLX error, fall back
                    # to the recorder's own faster-whisper transcribe for THIS utterance; after a
                    # few failures, drop MLX for the session.
                    self._recorder.wait_audio()
                    try:
                        text = self._transcribe_mlx(self.last_audio)
                        self._mlx_fails = 0
                    except Exception as me:
                        self._mlx_fails = getattr(self, "_mlx_fails", 0) + 1
                        print(f"[otto] MLX transcribe failed ({me!r}) — faster-whisper fallback",
                              file=sys.stderr, flush=True)
                        try:
                            text = self._recorder.transcribe()
                        except Exception:
                            text = ""
                        if self._mlx_fails >= 3:
                            self._use_mlx = False
                            print("[otto] disabling MLX for this session (repeated failures)",
                                  file=sys.stderr, flush=True)
                else:
                    text = self._recorder.text()  # blocks: (wake ->) record -> transcribe
            except Exception as e:
                # Don't hot-spin the CPU (and go silently deaf) on a persistent recorder error:
                # log it and back off briefly before retrying.
                import logging
                logging.getLogger("otto").warning("recorder error: %r", e)
                self._consec_errors += 1
                # Persistent failure (mic permission revoked, device unplugged) = we've gone
                # deaf. Tell the user ONCE instead of silently retrying forever.
                if (self._consec_errors >= 6 and not self._deaf_reported
                        and self._on_health is not None):
                    self._deaf_reported = True
                    self._post(self._on_health, "deaf")
                time.sleep(0.5)
                continue
            self._last_text = time.monotonic()   # .text() returned -> transcription path alive
            if self._deaf_reported and self._on_health is not None:
                self._post(self._on_health, "ok")   # recovered — clear the alert
            self._consec_errors = 0
            self._deaf_reported = False
            if text and self._running:
                self._post(self._on_final, text.strip())

    def _health_loop(self) -> None:
        """Rebuild the recorder if it breaks — either a worker process dies, or the parent
        starts spinning on a dead pipe (the EOFError flood). Both make Otto silently go deaf;
        this recovers without a manual restart. Checks every few seconds, with a cooldown."""
        while self._running:
            time.sleep(4)
            if not self._running:
                break
            rec = self._recorder
            if rec is None:
                continue
            if time.monotonic() - self._last_rebuild < 15:
                continue                              # don't rebuild-storm
            reason = self._unhealthy_reason(rec, time.monotonic())
            if reason:
                self._unhealthy_streak += 1
                print(f"[otto] recorder unhealthy ({reason}) — rebuilding",
                      file=sys.stderr, flush=True)
                self._rebuild_recorder()
                # If it can't recover after a couple of tries, TELL the user (dashboard/notify)
                # instead of leaving them in a silent not-listening state.
                if (self._unhealthy_streak >= 2 and not self._deaf_reported
                        and self._on_health is not None):
                    self._deaf_reported = True
                    self._post(self._on_health, "deaf")
            else:
                self._unhealthy_streak = 0
                if self._deaf_reported and self._on_health is not None:
                    self._deaf_reported = False
                    self._post(self._on_health, "ok")   # recorder recovered — clear the alert

    def _unhealthy_reason(self, rec, now: float) -> str:
        """Why the recorder needs rebuilding, or '' if it's fine. Pure/testable.

        - 'worker died': a transcription/reader child process exited.
        - 'transcription stalled': a recording finished but no transcript came back within the
          timeout — the pipe is wedged even though the worker is still 'alive' (the real EOF
          flood). Caught by symptom, since the child's own EOF logging isn't visible to us.
        - 'pipe errors': our own read loop saw a burst of pipe errors.
        """
        tp = getattr(rec, "transcript_process", None)
        rp = getattr(rec, "reader_process", None)
        if (tp is not None and not tp.is_alive()) or (rp is not None and not rp.is_alive()):
            return "worker died"
        # Worker NEVER came up: the mic pipeline failed to spawn at startup (a spawn race, or mic
        # access denied) — the recorder object exists but no child is listening, so Otto is silently
        # deaf. The check above only catches a worker that WAS alive and died; this catches one that
        # never started. Guarded by a startup grace so we don't fire while the children are still
        # forking. (The "0 recorder workers / not listening" state.)
        if (now - getattr(self, "_started_at", now)) > 20:
            tp_alive = tp is not None and tp.is_alive()
            rp_alive = rp is not None and rp.is_alive()
            if not tp_alive and not rp_alive:
                return "worker not running"
        if (self._last_record_stop > 0
                and now - self._last_record_stop > 12
                and self._last_text < self._last_record_stop):
            return "transcription stalled"
        if self._pipe_errors >= 5:
            return "pipe errors"
        return ""

    def _rebuild_recorder(self) -> None:
        """Tear down the (broken) recorder and build a fresh one from the saved params. Shutting
        the old one down unblocks _run's `.text()`, which then picks up the new recorder."""
        from RealtimeSTT import AudioToTextRecorder
        self._last_rebuild = time.monotonic()
        self._pipe_errors = 0
        self._last_record_stop = 0.0     # reset stall tracking for the fresh recorder
        self._last_text = time.monotonic()
        old = self._recorder
        try:
            if old is not None:
                old.shutdown()
        except Exception:
            pass
        try:
            self._recorder = AudioToTextRecorder(**self._params)
            print("[otto] recorder rebuilt", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"[otto] recorder rebuild failed: {e!r}", file=sys.stderr, flush=True)

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
        self._cancel_watchdog()
        if self._recorder is not None:
            try:
                self._recorder.shutdown()
            except Exception:
                pass
