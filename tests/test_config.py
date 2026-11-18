import pytest
from pydantic import ValidationError

from estatequery.config import ConfigError, Settings


def make(**overrides) -> Settings:
    """Build settings from explicit values only, ignoring any local .env file."""
    return Settings(_env_file=None, **overrides)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (
        "LLM_PROVIDER",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
        "OPENROUTER_API_KEY",
        "OPENROUTER_MODEL",
        "OPENROUTER_FALLBACK_MODELS",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_defaults_to_anthropic_for_development():
    settings = make()
    assert settings.llm_provider == "anthropic"
    assert settings.active_model == "claude-opus-5-5"


def test_switching_provider_changes_the_active_model():
    settings = make(llm_provider="openrouter", openrouter_model="vendor/model:free")
    assert settings.active_model == "vendor/model:free"


def test_provider_switch_reads_from_environment(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_MODEL", "vendor/model:free")
    assert make().active_model == "vendor/model:free"


def test_unknown_provider_is_rejected():
    with pytest.raises(ValidationError):
        make(llm_provider="something-else")


def test_fallback_models_are_parsed_in_order():
    settings = make(openrouter_fallback_models=" a/one:free, b/two:free ,, ")
    assert settings.openrouter_fallbacks == ["a/one:free", "b/two:free"]


@pytest.mark.parametrize(
    ("overrides", "missing"),
    [
        ({"llm_provider": "anthropic"}, "ANTHROPIC_API_KEY"),
        ({"llm_provider": "anthropic", "anthropic_api_key": "   "}, "ANTHROPIC_API_KEY"),
        ({"llm_provider": "openrouter"}, "OPENROUTER_API_KEY"),
        ({"llm_provider": "openrouter", "openrouter_api_key": "k"}, "OPENROUTER_MODEL"),
    ],
)
def test_startup_check_names_the_missing_setting(overrides, missing):
    with pytest.raises(ConfigError, match=missing):
        make(**overrides).require_provider_ready()


def test_startup_check_passes_when_the_active_provider_is_configured():
    make(llm_provider="anthropic", anthropic_api_key="k").require_provider_ready()
    make(
        llm_provider="openrouter", openrouter_api_key="k", openrouter_model="vendor/model:free"
    ).require_provider_ready()


def test_tracing_is_off_until_both_langfuse_keys_are_set():
    assert not make().tracing_enabled
    assert not make(langfuse_public_key="pk").tracing_enabled
    assert make(langfuse_public_key="pk", langfuse_secret_key="sk").tracing_enabled


def test_keys_are_not_exposed_when_settings_are_printed():
    settings = make(anthropic_api_key="super-secret-value")
    assert "super-secret-value" not in repr(settings)
