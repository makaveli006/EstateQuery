"""Application settings.

This module is the only place environment variables are read. Everything else
receives a ``Settings`` object, which keeps configuration testable and makes the
provider switch a one-line change in the environment.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Provider = Literal["anthropic", "openrouter"]


class ConfigError(RuntimeError):
    """Raised when the active configuration cannot run the app."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["dev", "prod"] = "dev"

    # LLM provider switch
    llm_provider: Provider = "anthropic"

    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"

    openrouter_api_key: SecretStr | None = None
    openrouter_model: str = ""
    openrouter_fallback_models: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # Tracing
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_base_url: str = "https://cloud.langfuse.com"

    # Limits and guardrails
    max_input_chars: int = Field(default=1000, gt=0)
    max_tool_rounds: int = Field(default=4, gt=0)
    rate_limit_per_minute: int = Field(default=10, gt=0)
    daily_request_cap: int = Field(default=800, gt=0)
    reranker_enabled: bool = False

    data_dir: Path = Path("data")

    @property
    def active_model(self) -> str:
        """Model ID used by the currently selected provider."""
        if self.llm_provider == "anthropic":
            return self.anthropic_model
        return self.openrouter_model

    @property
    def openrouter_fallbacks(self) -> list[str]:
        """Backup OpenRouter models, in the order they should be tried."""
        return [m.strip() for m in self.openrouter_fallback_models.split(",") if m.strip()]

    @property
    def tracing_enabled(self) -> bool:
        return bool(_secret(self.langfuse_public_key) and _secret(self.langfuse_secret_key))

    def require_provider_ready(self) -> None:
        """Fail fast, with a clear message, if the selected provider cannot run.

        Called at app startup rather than at import, so ingestion, indexing and
        unit tests work without any API key.
        """
        if self.llm_provider == "anthropic":
            if not _secret(self.anthropic_api_key):
                raise ConfigError("LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set")
            if not self.anthropic_model:
                raise ConfigError("LLM_PROVIDER=anthropic but ANTHROPIC_MODEL is empty")
            return
        if not _secret(self.openrouter_api_key):
            raise ConfigError("LLM_PROVIDER=openrouter but OPENROUTER_API_KEY is not set")
        if not self.openrouter_model:
            raise ConfigError("LLM_PROVIDER=openrouter but OPENROUTER_MODEL is empty")


def _secret(value: SecretStr | None) -> str:
    return value.get_secret_value().strip() if value else ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
