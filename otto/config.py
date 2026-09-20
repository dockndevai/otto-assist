"""Configuration + storage locations.

Otto keeps its config and data in a per-user directory (default
``~/Library/Application Support/Otto``; override with ``OTTO_HOME``). On first run the
directory is created and ``config.yaml`` is seeded from the packaged template. Values come
from (lowest to highest priority): built-in defaults -> config.yaml -> environment
variables (``OTTO_*`` with ``__`` as the nesting separator, e.g. ``OTTO_BRAIN__MODEL=opus``).

This file is *wiring* config only — how input/output flows. Agent behavior (MCP servers,
subagents, skills, CLAUDE.md, permissions) comes from the user's own Claude config.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PACKAGED_DEFAULT = PROJECT_ROOT / "config.yaml"


def _resolve_home() -> Path:
    env = os.environ.get("OTTO_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / "Library" / "Application Support" / "Otto"


OTTO_HOME = _resolve_home()
DATA_CONFIG = OTTO_HOME / "config.yaml"


def _seed_config() -> None:
    try:
        OTTO_HOME.mkdir(parents=True, exist_ok=True)
        if not DATA_CONFIG.exists() and PACKAGED_DEFAULT.exists():
            DATA_CONFIG.write_text(PACKAGED_DEFAULT.read_text())
    except Exception:
        pass


_seed_config()
# Prefer the per-user config; fall back to the packaged template if seeding failed.
CONFIG_FILE = DATA_CONFIG if DATA_CONFIG.exists() else PACKAGED_DEFAULT


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    # Remote access for the mobile app. OFF by default — the server stays loopback-only and
    # behaves exactly as before. When enabled, the server ALSO binds beyond loopback (so a phone
    # on the LAN or over a Tailscale/tunnel address can reach it) and accepts connections that
    # present the bearer token: `Authorization: Bearer <token>` (HTTP + WS) or `?token=<token>`
    # (WS fallback for clients that can't set headers). Loopback browser access is unchanged.
    allow_remote: bool = False
    auth_token: str = ""        # shared secret for remote clients; auto-filled when remote is on

    def bind_host(self) -> str:
        """Host uvicorn should bind. Loopback-only unless remote access is enabled, in which
        case bind all interfaces so LAN / Tailscale / tunnel addresses can reach it (every
        non-loopback connection still needs the token)."""
        if self.allow_remote and self.host in ("127.0.0.1", "localhost"):
            return "0.0.0.0"
        return self.host


class BrainConfig(BaseModel):
    type: Literal["claude"] = "claude"          # pluggable brain; only Claude for now
    model: str = ""                             # "" -> use Claude's default model
    # Session access level — how Otto handles Claude's permission prompts for consequential tools.
    # Change it in the dashboard (Settings → Session access level). Default is full access so a
    # voice/phone session isn't blocked mid-task waiting for a spoken yes.
    #   bypass -> full access, never asks (SDK bypassPermissions); auto -> approve via callback;
    #   voice  -> Otto asks you aloud/on-screen; deny -> reject writes (read-only).
    # bypass/auto = never ask (SDK auto-approves). guarded = do everything asked, but confirm
    # genuinely DANGEROUS actions (destructive shell, delete/erase, outbound send, payments).
    # voice = confirm every consequential action. deny = block all writes.
    permission: Literal["bypass", "auto", "guarded", "voice", "deny"] = "guarded"
    project_dir: str = "~"                       # current project (the brain's cwd)
    projects: list[str] = Field(default_factory=list)   # pinned projects for the switcher
    # Last-used chat (Claude session id) per project path — so switching back to a project
    # reopens the chat you were in. Written by the orchestrator when a chat is used/switched.
    sessions: dict[str, str] = Field(default_factory=dict)
    extra_dirs: list[str] = Field(default_factory=list)  # extra readable dirs (~ expanded)
    use_claude_config: bool = True              # load the user's ~/.claude settings/MCP/agents
    # Agentic tools whose actions run WITHOUT a per-step spoken confirmation, so a computer-use
    # / browser loop isn't interrupted on every click. (Destructive shell + write-verb connector
    # calls still confirm; the model is told to ask before genuinely consequential steps.) These
    # are tool-name prefixes; clear the list to force confirmation on everything.
    auto_allow_tools: list[str] = Field(
        default_factory=lambda: ["mcp__playwright__", "mcp__computer-use__",
                                 "mcp__claude-in-chrome__", "mcp__macos__", "mcp__desktop__"])
    continue_conversation: bool = False          # resume the project's latest Claude session
    register_mcp: bool = True                   # expose /mcp for `claude mcp add otto`
    # Give the brain FULL desktop control (click/type/focus/open any app — Finder, VS Code,
    # Chrome, …) by loading the mac-use-mcp "computer-use" server directly in the brain (so it's
    # guaranteed available, not subject to Claude Code's per-config MCP approval). Needs macOS
    # Accessibility + Screen Recording. Set the command/args if you use a different automation MCP.
    computer_use: bool = True
    computer_use_command: str = "npx"
    computer_use_args: list[str] = Field(default_factory=lambda: ["-y", "mac-use-mcp"])


class STTConfig(BaseModel):
    model: str = "small.en"
    language: str = "en"
    # Multilingual Whisper model used when voice.language is non-English (Hinglish/Hindi/regional).
    # "small" is the CPU sweet spot; "medium" is more accurate but slow without a GPU.
    multilingual_model: str = "small"
    # How long a pause (seconds) ends your turn. Higher = Otto waits longer for you to
    # finish, so it won't cut you off mid-thought. Lower = snappier but interrupts.
    # 1.0s keeps it responsive; the instant spoken ack covers the rest of the wait.
    silence_seconds: float = 1.0
    # Live captions: stream partial words to the dashboard as you speak, using a small fast
    # model. OFF by default: RealtimeSTT's realtime worker can fall into an EOFError spam loop
    # (esp. under some Python/multiprocessing setups) that destabilizes the recorder. The
    # dashboard still shows a "Listening…" state and your final transcript without it.
    realtime_partials: bool = False
    realtime_model: str = "base.en"
    # Respond to everything you say, no wake word needed (just talk). Otto still ignores
    # its own voice; mute the mic (menu bar) when you don't want it listening.
    always_listen: bool = False
    # Clean up the raw transcript (filler words, false starts, mis-hears) with a cheap model
    # pass before it's used. Off by default (adds a little latency); most useful for dictation.
    refine: bool = False
    refine_model: str = "haiku"
    # Dictation-anywhere: type transcribed speech into the focused app (⌘V, clipboard-safe)
    # instead of sending it to Claude. Runtime-toggleable; this is the startup default.
    dictation_mode: bool = False
    # After a reply, keep listening this many seconds for a follow-up WITHOUT the wake word, so
    # a back-and-forth doesn't need "hey otto" every turn. 0 = off (always require the wake word).
    follow_up_seconds: float = 0.0
    # Anti-hallucination: drop Whisper's noise output so ambient sound isn't run as a command.
    #   filter_hallucinations: free heuristic (empty/short/repetitive/filler) on every voice turn.
    #   hands_free_verify: a cheap model gate on the HANDS-FREE path (always-listen / follow-up)
    #     that also rejects grammatical-but-nonsensical hallucinations. Adds a little latency to
    #     hands-free turns only; the wake-word/push-to-talk paths stay snappy.
    filter_hallucinations: bool = True
    hands_free_verify: bool = True
    verify_model: str = "haiku"
    # VAD tightening (RealtimeSTT): lower silero_sensitivity + a longer minimum recording make
    # the recorder trigger on clearer speech and ignore short noise blips.
    silero_sensitivity: float = 0.4
    min_length_of_recording: float = 0.5
    # #57 continuous-audio robustness: run faster-whisper's own VAD filter so non-speech spans
    # (music/podcast bleeding into the mic) are dropped BEFORE transcription — far fewer
    # hallucinated "commands" from ambient audio. On by default; the biggest reported STT pain.
    vad_filter: bool = True
    # Use silero (not just webrtc) to detect END of speech too — steadier turn-ends when there's
    # background noise, so the recorder doesn't run one endless capture over continuous audio.
    # Costs a little CPU; off by default (the max_recording_seconds cap already backstops wedges).
    silero_deactivity_detection: bool = False
    # Cap a single capture: without a natural pause the VAD never ends a recording (e.g. a
    # video/podcast playing into the mic), so force-finalize after this many seconds. Keeps
    # Otto answering back-to-back asks instead of getting stuck on one endless utterance.
    max_recording_seconds: float = 15.0
    # On the wake word, pause whatever media is playing (YouTube/Spotify/Music) so the mic
    # hears only you — then resume it when the listen window ends with no command. Uses the
    # system play/pause media key, so it doesn't mute Orrin's own voice.
    duck_media_on_wake: bool = True
    # Voice barge-in: keep the mic LISTENING while Orrin is speaking so you can say "stop" to
    # cut it off mid-sentence. Only stop-words act during playback; everything else (incl. its
    # own echo) is dropped. Reliable on headphones; on laptop speakers the mic hears Orrin's own
    # voice so it's best-effort (use the Stop button/hotkey there). Off = mic muted during
    # speech (voice "stop" only works in gaps). Default OFF as a manual override.
    barge_in: bool = False
    # Auto barge-in (#56): enable barge-in automatically ONLY when the output route is acoustically
    # safe — headphones / AirPods / an external device, where the mic can't hear Orrin's own voice.
    # On the built-in speakers it stays half-duplex (no echo loop). So plugging in headphones gives
    # clean mid-speech voice "stop" with zero config, and unplugging reverts to safe half-duplex.
    barge_in_auto: bool = True
    # Wake-word backend (default "phrase": account-free software wake word, reliable). Note the
    # trade-off — "phrase" transcribes ALL audio continuously (heavier idle CPU). Account-free
    # gate alternatives exist but need per-machine tuning/verification before defaulting to them:
    #   "oww"         — openWakeWord built-in ("hey_jarvis", …); a true gate, but this RealtimeSTT
    #                    build loads ALL built-in models and ignores the wake_words filter, so it
    #                    needs voice tuning (load one model + threshold) before it's the default.
    #   "efficientword" — few-shot custom word via `python -m otto.enroll_wake_word` (no account);
    #                    also transcribes continuously (not a gate).
    #   "pvporcupine" — custom .ppn keyword, needs a Picovoice AccessKey (external account).
    wakeword_backend: str = "phrase"
    wake_words: str = "hey otto"
    wake_word_sensitivity: float = 0.6
    # Speak a greeting ("what can I do?") when the wake word fires. OFF by default: it's
    # repetitive, and on a speaker it feeds the wake detector its own voice → a greet loop.
    # When off, Orrin just starts listening (shown on the dashboard) and acts on your command.
    wake_greeting: bool = False
    # Ignore repeat wake-word fires within this many seconds (debounce the detector so it can't
    # re-trigger on echo/ambient and spam the listen state).
    wake_debounce_seconds: float = 3.0
    # For backend "oww": path to a custom openWakeWord model (.onnx/.tflite), e.g. a
    # trained "hey otto". Empty = use openWakeWord's built-in phrases via `wake_words`.
    oww_model_path: str = ""
    oww_inference_framework: str = "onnx"   # "onnx" or "tflite" (match your model file)
    # For backend "efficientword": a few-shot custom wake word anyone can make by recording
    # their phrase (run `python -m otto.enroll_wake_word`). Points at the generated *_ref.json.
    eff_reference: str = ""
    eff_threshold: float = 0.70


class TTSConfig(BaseModel):
    # "system" = macOS `say` (default, zero setup); "kokoro" = local neural voice (offline,
    # no key — needs `kokoro-onnx` + model files, see `python -m otto.voice.fetch_kokoro`);
    # "elevenlabs" = premium cloud voice (needs ELEVENLABS_API_KEY).
    engine: Literal["system", "elevenlabs", "kokoro"] = "system"
    voice: str = ""                              # macOS `say` voice (engine "system")
    # Kokoro (engine "kokoro"): a voice id (see otto.voice.tts.KOKORO_VOICES), speaking speed,
    # and where the model files live (blank = OTTO_HOME/models/kokoro).
    kokoro_voice: str = "af_heart"
    kokoro_speed: float = 0.92          # slightly slower than 1.0 → more natural, less rushed
    kokoro_model_dir: str = ""
    # Cap how much of a reply is spoken aloud, as a backstop against a runaway verbose reply.
    # The model is already told to answer in 1-2 sentences WITH closure, so this should rarely
    # bite; keep it generous so a normal concise reply is spoken in full (not truncated). 0
    # disables the cap. Markdown/URLs/code are stripped from speech regardless.
    max_spoken_chars: int = 600
    # When a phone (authenticated remote client) is connected, don't also speak the reply
    # aloud on the Mac — the phone speaks it. Avoids Otto talking to an empty room when you're
    # driving it remotely. The dashboard on the Mac itself does NOT count as remote.
    mute_when_remote: bool = True
    # Per-project voice override (project path -> voice id/name). When you focus a project,
    # Orrin speaks in its voice — handy for telling parallel workspaces apart. Falls back to
    # `voice`/`kokoro_voice` for projects with no entry.
    project_voices: dict[str, str] = Field(default_factory=dict)
    # Kokoro's voices are English; for non-English speech (Hinglish/Hindi/regional) fall back to
    # macOS `say` with a language-appropriate voice. Map of voice.language -> `say` voice name.
    # (Rishi = Indian English, Lekha = Hindi; install more in System Settings › Spoken Content.)
    lang_voices: dict[str, str] = Field(
        default_factory=lambda: {"en-IN": "Rishi", "hi": "Lekha", "hinglish": "Rishi"})


class HotkeyConfig(BaseModel):
    # Global push-to-talk. Under the menu-bar app it's a main-thread NSEvent monitor (works in
    # the packaged .app — the old pynput backend SIGTRAP-crashed it); a bare terminal run falls
    # back to pynput. OFF by default because it needs macOS Input Monitoring permission (which,
    # like the mic, doesn't persist across rebuilds on an ad-hoc-signed build). The dashboard
    # Talk button and the wake word cover push-to-talk without it.
    enabled: bool = False
    push_to_talk: str = "<cmd>+<shift>+j"


class VoiceConfig(BaseModel):
    input_enabled: bool = True
    output_enabled: bool = True
    # Spoken language / dialect. Drives STT model + Whisper language, a persona hint (Claude
    # replies in kind), and the spoken voice. Values:
    #   "en"       — English only, fastest, best accent-agnostic (small.en)          [default]
    #   "en-IN"    — Indian English (small.en + Indian-name priming; Indian voice)
    #   "hinglish" — Hindi/English code-switch (multilingual model, auto-detect)
    #   "hi"       — Hindi          |  "auto" — auto-detect  |  any Whisper code (e.g. "ta")
    # Non-English values switch STT to the multilingual model (heavier on CPU, no GPU here).
    language: str = "en"
    stt: STTConfig = Field(default_factory=STTConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    hotkey: HotkeyConfig = Field(default_factory=HotkeyConfig)
    # On app open, if you don't say anything within a few seconds, Orrin greets you and gives a
    # short spoken briefing (last session recap + anything pending + one suggestion). Cancelled
    # the moment you speak or type. Fires once per launch, only for the focused project.
    startup_briefing: bool = True
    startup_briefing_delay: float = 6.0
    # Idle background research: after this many seconds with no input, Orrin quietly researches
    # ONE project at a time (read-only — investigate + save findings to memory, never edit),
    # reaches a conclusion, then cycles to the next. Pauses the instant you speak or type.
    idle_research: bool = True
    idle_research_delay: float = 300.0        # ~5 min of quiet before it kicks in
    idle_research_readonly: bool = True       # hard read-only (recommended; do not disable)
    idle_research_min_gap: float = 1800.0     # don't re-research a project within 30 min
    idle_research_max_projects: int = 0       # 0 = all discovered projects, in rotation
    # Weight research-project selection by recent git activity, so attention tracks where you're
    # actually working (breaks ties among eligible projects toward recently-committed ones).
    idle_research_git_weight: bool = True
    # Keep researching in the background even while "asleep" (paused via "stop listening"). It's
    # silent (never speaks), so it's safe while paused; set False to pause research when you sleep it.
    idle_research_while_asleep: bool = True
    # Rotate the research objective: every Nth pass proposes a NEW feature/improvement with a short
    # plan (instead of hunting risks/next-steps). 0 disables feature passes; 3 = ~1 in 3.
    idle_research_feature_every: int = 3
    # Daily cross-project roadmap: once a day (at/after morning_brief_hour) synthesize all findings
    # + open suggestions into a prioritized plan, saved to memory/roadmap.md and spoken in the brief.
    roadmap_synthesis: bool = True
    # Daily "morning brief": once a day, on first quiet moment at/after this local hour, Orrin
    # gives a spoken cross-project briefing synthesizing overnight findings + open suggestions.
    # Set morning_brief=False to disable. Cancelled the instant you speak/type, like the startup
    # briefing; fires at most once per calendar day.
    morning_brief: bool = True
    morning_brief_hour: int = 9               # local hour (0-23) the brief becomes eligible


class MemoryConfig(BaseModel):
    """Orrin's persistent, cross-session memory (lives under OTTO_HOME/memory).

    Every Orrin session shares this store, so what it learns in one conversation is available
    in the next — and per project, so each project keeps its own knowledge. Two mechanisms:
    a bounded *preamble* injected into every turn (durable facts + a rolling summary), and an
    automatic *capture* pass after each turn that decides what's worth remembering.
    """
    enabled: bool = True
    # Prepend the memory preamble (global + project facts + recent summary) to each turn so
    # continuity is automatic. Bounded by max_preamble_chars, so context cost stays small.
    inject: bool = True
    # After every turn, a background pass extracts durable facts + a one-line summary and
    # writes them to the store — fully automatic, no explicit "remember" needed.
    auto_capture: bool = True
    # Upper bound on the injected preamble (characters). Project + summary are trimmed first.
    max_preamble_chars: int = 2000
    # How many recent one-line summaries to keep (the rolling "Recently" section).
    max_summary_lines: int = 15
    # Fast/cheap model for the capture pass (an alias the user's Claude accepts). The capture
    # runs off the voice path, so latency here doesn't affect responsiveness.
    capture_model: str = "haiku"
    # Add the project's memory dir to Claude's readable dirs, so it can open a saved document
    # in full (beyond the short excerpt injected into the preamble) when it needs the detail.
    expose_to_claude: bool = True
    # Cross-project awareness: keep a registry of every project and, on "what should we do /
    # across my projects" questions, inject a bounded digest spanning all of them.
    cross_project: bool = True
    max_overview_chars: int = 4000
    # Also add the whole memory/projects dir to Claude's readable dirs so it can read a sibling
    # project's notes verbatim. Off by default — the injected digest covers the common case.
    expose_overview_dirs: bool = False
    # Actionable-suggestion queue: background research saves each actionable suggestion / risk as
    # a tracked item (open → done/dismissed/stale) so Orrin can surface and act on them. Risks at
    # `high` severity are escalated immediately (notification + dashboard alert) when found.
    suggestions: bool = True
    risk_alerts: bool = True                  # notify + dashboard alert on new high-severity risks
    suggestion_stale_days: float = 14.0       # open suggestions older than this are aged out
    max_open_suggestions: int = 50


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OTTO_",
        env_nested_delimiter="__",
        yaml_file=str(CONFIG_FILE),
        extra="ignore",
    )

    server: ServerConfig = Field(default_factory=ServerConfig)
    brain: BrainConfig = Field(default_factory=BrainConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Priority (high -> low): env vars, then config.yaml, then defaults.
        return (env_settings, YamlConfigSettingsSource(settings_cls))

    @property
    def data_dir(self) -> Path:
        OTTO_HOME.mkdir(parents=True, exist_ok=True)
        return OTTO_HOME

    @property
    def project_path(self) -> Path:
        return Path(self.brain.project_dir).expanduser()

    @property
    def add_dirs(self) -> list[str]:
        return [str(Path(d).expanduser()) for d in self.brain.extra_dirs]


def load_settings() -> Settings:
    """Defaults <- config.yaml <- OTTO_* env vars (highest priority)."""
    return Settings()


def config_path() -> Path:
    """The config file the settings UI reads/writes (the per-user one when available)."""
    return CONFIG_FILE


def save_config(data: dict) -> None:
    """Persist a full config mapping to the per-user config.yaml."""
    target = DATA_CONFIG if str(CONFIG_FILE) == str(DATA_CONFIG) else CONFIG_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def load_env() -> None:
    """Minimal .env loader (KEY=VALUE lines) into os.environ, without a dependency."""
    for candidate in (OTTO_HOME / ".env", PROJECT_ROOT / ".env"):
        if not candidate.exists():
            continue
        for line in candidate.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())
