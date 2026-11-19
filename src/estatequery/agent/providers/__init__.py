from __future__ import annotations

from estatequery.agent.providers.base import Provider, ProviderError
from estatequery.config import ModelSpec


def get_provider(spec: ModelSpec) -> Provider:
    """Return the adapter for a model's provider. Imports lazily so one missing key
    or SDK does not stop the other provider from working."""
    if spec.provider == "anthropic":
        from estatequery.agent.providers.anthropic import AnthropicProvider

        return AnthropicProvider()
    if spec.provider == "openrouter":
        from estatequery.agent.providers.openrouter import OpenRouterProvider

        return OpenRouterProvider()
    raise ProviderError(f"Unknown provider '{spec.provider}'.")


__all__ = ["Provider", "ProviderError", "get_provider"]
