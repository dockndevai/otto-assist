"""Configuration loader.

Values come from (lowest to highest priority): built-in defaults -> config.yaml ->
environment variables (JARVIS_* with `__` as the nesting separator, e.g.
JARVIS_BRAIN__MODEL=opus).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_ROOT / "config.yaml"


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000


class BrainConfig(BaseModel):
    model: str = "sonnet"
    permission: Literal["ask", "auto"] = "ask"
    workspace: str = "workspace"


class STTConfig(BaseModel):
    model: str = "small.en"
    language: str = "en"
    wakeword_backend: str = "oww"
    wake_words: str = "hey jarvis"
    wake_word_sensitivity: float = 0.6


class TTSConfig(BaseModel):
    engine: Literal["system", "elevenlabs"] = "system"
    voice: str = ""


class HotkeyConfig(BaseModel):
    push_to_talk: str = "<cmd>+<shift>+j"


class VoiceConfig(BaseModel):
    input_enabled: bool = True
    output_enabled: bool = True
    stt: STTConfig = Field(default_factory=STTConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    hotkey: HotkeyConfig = Field(default_factory=HotkeyConfig)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JARVIS_",
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
    def workspace_path(self) -> Path:
        p = PROJECT_ROOT / self.brain.workspace
        p.mkdir(parents=True, exist_ok=True)
        return p


def load_settings() -> Settings:
    """Defaults <- config.yaml <- JARVIS_* env vars (highest priority)."""
    return Settings()


def load_env() -> None:
    """Minimal .env loader (KEY=VALUE lines) into os.environ, without a dependency."""
    import os

    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())
