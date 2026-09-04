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


class BrainConfig(BaseModel):
    type: Literal["claude"] = "claude"          # pluggable brain; only Claude for now
    model: str = ""                             # "" -> use Claude's default model
    # How to handle Claude's permission prompts for consequential tools:
    #   voice -> Otto asks you aloud/on-screen; auto -> approve; deny -> reject
    permission: Literal["voice", "auto", "deny"] = "voice"
    project_dir: str = "~"                       # current project (the brain's cwd)
    projects: list[str] = Field(default_factory=list)   # pinned projects for the switcher
    # Last-used chat (Claude session id) per project path — so switching back to a project
    # reopens the chat you were in. Written by the orchestrator when a chat is used/switched.
    sessions: dict[str, str] = Field(default_factory=dict)
    extra_dirs: list[str] = Field(default_factory=list)  # extra readable dirs (~ expanded)
    use_claude_config: bool = True              # load the user's ~/.claude settings/MCP/agents
    continue_conversation: bool = False          # resume the project's latest Claude session
    register_mcp: bool = True                   # expose /mcp for `claude mcp add otto`


class STTConfig(BaseModel):
    model: str = "small.en"
    language: str = "en"
    # How long a pause (seconds) ends your turn. Higher = Otto waits longer for you to
    # finish, so it won't cut you off mid-thought. Lower = snappier but interrupts.
    # ~1.0s is a good balance: responsive without clipping normal pauses.
    silence_seconds: float = 1.0
    # Live captions: stream partial words to the dashboard as you speak (so you can SEE it's
    # hearing you), using a small fast model. Turn off to save CPU on an always-on machine.
    realtime_partials: bool = True
    realtime_model: str = "base.en"
    # Respond to everything you say, no wake word needed (just talk). Otto still ignores
    # its own voice; mute the mic (menu bar) when you don't want it listening.
    always_listen: bool = False
    # "phrase" = software wake word (any phrase, no account); "oww" (openWakeWord built-ins
    # like "hey jarvis"); "pvporcupine" (custom .ppn, needs a Picovoice key).
    wakeword_backend: str = "phrase"
    wake_words: str = "hey otto"
    wake_word_sensitivity: float = 0.6
    # For backend "oww": path to a custom openWakeWord model (.onnx/.tflite), e.g. a
    # trained "hey otto". Empty = use openWakeWord's built-in phrases via `wake_words`.
    oww_model_path: str = ""
    oww_inference_framework: str = "onnx"   # "onnx" or "tflite" (match your model file)
    # For backend "efficientword": a few-shot custom wake word anyone can make by recording
    # their phrase (run `python -m otto.enroll_wake_word`). Points at the generated *_ref.json.
    eff_reference: str = ""
    eff_threshold: float = 0.70


class TTSConfig(BaseModel):
    engine: Literal["system", "elevenlabs"] = "system"
    voice: str = ""


class HotkeyConfig(BaseModel):
    # The global push-to-talk hotkey (pynput) is OFF by default: it needs macOS
    # Accessibility permission, and pynput's Carbon input-source calls run on a background
    # thread, which aborts (SIGTRAP) inside the packaged .app. The mic still works via the
    # dashboard button and the wake word; enable this only if you've granted Accessibility.
    enabled: bool = False
    push_to_talk: str = "<cmd>+<shift>+j"


class VoiceConfig(BaseModel):
    input_enabled: bool = True
    output_enabled: bool = True
    stt: STTConfig = Field(default_factory=STTConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    hotkey: HotkeyConfig = Field(default_factory=HotkeyConfig)


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
