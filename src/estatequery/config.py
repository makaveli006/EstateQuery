"""Central configuration: environment settings and the model registry."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings,
                                   file_secret_settings):
        # A project .env beats ambient shell variables, so stale keys exported for another
        # project on a developer machine cannot leak in. In Docker there is no .env file
        # in the image, so the container's environment variables are what gets used.
        return init_settings, dotenv_settings, env_settings, file_secret_settings

    openrouter_api_key: str = ""
    anthropic_api_key: str = ""

    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_base_url: str = "https://cloud.langfuse.com"

    # Where scraped data and the built SQLite database live.
    data_dir: Path = Path("data")

    # Model shown first in the UI. Must be an id from MODELS.
    default_model: str = "openrouter:deepseek/deepseek-v4-flash"

    # Abuse protection for the public URL.
    rate_limit_per_minute: int = 10
    rate_limit_per_day: int = 100
    daily_cap_openrouter: int = 1500
    daily_cap_anthropic: int = 400
    max_message_chars: int = 2000
    max_history_messages: int = 12

    # Agent loop.
    max_tool_rounds: int = 3

    @property
    def db_path(self) -> Path:
        return self.data_dir / "estatequery.db"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"


@lru_cache
def get_settings() -> Settings:
    return Settings()


@dataclass(frozen=True)
class ModelSpec:
    id: str  # "<provider>:<api model name>", used by the UI and API
    provider: str  # "openrouter" | "anthropic"
    api_model: str  # the name sent to the provider
    label: str  # shown in the dropdown
    note: str = ""
    # USD per million tokens (input, output); used only for cost estimates in the eval report.
    price_in: float = 0.0
    price_out: float = 0.0


MODELS: list[ModelSpec] = [
    ModelSpec(
        "openrouter:deepseek/deepseek-v4-flash",
        "openrouter",
        "deepseek/deepseek-v4-flash",
        "DeepSeek V4 Flash",
        "OpenRouter",
        0.09,
        0.18,
    ),
    ModelSpec(
        "openrouter:google/gemini-3.8-flash",
        "openrouter",
        "google/gemini-3.8-flash",
        "Gemini 3.8 Flash",
        "OpenRouter",
        0.83,
        4.12,
    ),
    ModelSpec(
        "openrouter:google/gemma-4-31b-it:free",
        "openrouter",
        "google/gemma-4-31b-it:free",
        "Gemma 4 31B (free)",
        "OpenRouter free tier",
    ),
    ModelSpec(
        "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free",
        "openrouter",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "Nemotron 3 Ultra (free)",
        "OpenRouter free tier",
    ),
    ModelSpec(
        "anthropic:claude-sonnet-5-5",
        "anthropic",
        "claude-sonnet-5-5",
        "Claude Sonnet 5.5",
        "Anthropic API",
        2.0,
        10.0,
    ),
    ModelSpec(
        "anthropic:claude-opus-5-5",
        "anthropic",
        "claude-opus-5-5",
        "Claude Opus 5.5",
        "Anthropic API",
        4.0,
        20.0,
    ),
    ModelSpec(
        "anthropic:claude-haiku-5-5",
        "anthropic",
        "claude-haiku-5-5",
        "Claude Haiku 5.5",
        "Anthropic API",
        0.10,
        0.50,
    ),
]

MODELS_BY_ID: dict[str, ModelSpec] = {m.id: m for m in MODELS}


def get_model(model_id: str | None) -> ModelSpec:
    """Resolve a model id from the client, falling back to the configured default."""
    settings = get_settings()
    if model_id and model_id in MODELS_BY_ID:
        return MODELS_BY_ID[model_id]
    return MODELS_BY_ID.get(settings.default_model, MODELS[0])
