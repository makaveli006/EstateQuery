"""Anthropic adapter: Claude via the official SDK, streaming, with client-side tools."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, AsyncIterator

import anthropic
from anthropic import AsyncAnthropic

from estatequery.agent.providers.base import ProviderError, StepDone, TextDelta, ToolCall, ToolResult, Usage
from estatequery.config import get_settings

log = logging.getLogger("estatequery.providers.anthropic")

# Adaptive thinking shares this budget with the visible answer, so leave headroom.
MAX_TOKENS = 16000
# Tool-grounded Q&A is routine work; low effort keeps the chat responsive.
EFFORT = "low"
# Server-side refusal fallback (beta). Supported on Opus 5.5; Haiku 5.5 has no fallback.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}


@lru_cache
def _client() -> AsyncAnthropic:
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise ProviderError("ANTHROPIC_API_KEY is not configured.")
    return AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=90.0, max_retries=1)


def _to_anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted = []
    for t in tools:
        schema = {**t["parameters"], "additionalProperties": False}
        tool: dict[str, Any] = {"name": t["name"], "description": t["description"], "input_schema": schema}
        # Strict mode caps the number of optional parameters per request (24), and the
        # search filters alone exceed that. So only tools whose parameters are all
        # required are strict; the rest are validated by run_tool on our side.
        if set(schema.get("required", [])) == set(schema.get("properties", {})):
            tool["strict"] = True
        converted.append(tool)
    return converted


class AnthropicSession:
    def __init__(self, model: str, system: str, history: list[dict[str, str]], user_message: str,
                 tools: list[dict[str, Any]]) -> None:
        self.model = model
        self.tools = _to_anthropic_tools(tools)
        # The system prompt and tool list are identical on every call, so cache them.
        self.system = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        self.messages: list[dict[str, Any]] = [{"role": m["role"], "content": m["content"]} for m in history]
        self.messages.append({"role": "user", "content": user_message})

    def _stream(self, allow_tools: bool) -> Any:
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": self.system,
            "tools": self.tools,
            "tool_choice": {"type": "auto"} if allow_tools else {"type": "none"},
            "messages": self.messages,
            "output_config": {"effort": EFFORT},
        }
        client = _client()
        if self.model in FALLBACK_MODELS:
            return client.beta.messages.stream(**params, betas=[FALLBACK_BETA], fallbacks="default")
        return client.messages.stream(**params)

    async def step(self, *, allow_tools: bool) -> AsyncIterator[TextDelta | StepDone]:
        try:
            async with self._stream(allow_tools) as stream:
                async for event in stream:
                    if event.type == "text":
                        yield TextDelta(event.text)
                message = await stream.get_final_message()
        except anthropic.RateLimitError as exc:
            raise ProviderError("Claude is rate limited right now. Try again shortly.", retryable=True) from exc
        except anthropic.AuthenticationError as exc:
            raise ProviderError("Anthropic rejected the API key.") from exc
        except anthropic.APIStatusError as exc:
            log.warning("Anthropic API error %s for %s: %s", exc.status_code, self.model, exc.message)
            raise ProviderError(f"Anthropic returned an error ({exc.status_code}).",
                                retryable=exc.status_code >= 500) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError("Could not reach Anthropic.", retryable=True) from exc

        text = "".join(block.text for block in message.content if block.type == "text")
        usage = Usage(
            (message.usage.input_tokens or 0)
            + (getattr(message.usage, "cache_read_input_tokens", 0) or 0)
            + (getattr(message.usage, "cache_creation_input_tokens", 0) or 0),
            message.usage.output_tokens or 0,
        )

        if message.stop_reason == "refusal":
            yield StepDone(text=text or "The model declined to answer this request.", usage=usage,
                           stop_reason="refusal", model=message.model)
            return

        tool_calls: list[ToolCall] = []
        if message.stop_reason == "tool_use":
            for block in message.content:
                if block.type == "tool_use":
                    args = block.input if isinstance(block.input, dict) else None
                    tool_calls.append(ToolCall(id=block.id, name=block.name, args=args))
        # Append the full content, unchanged: thinking blocks must round-trip within a turn.
        self.messages.append({"role": "assistant", "content": message.content})
        yield StepDone(text=text, tool_calls=tool_calls, usage=usage, stop_reason=message.stop_reason,
                       model=message.model)

    def add_tool_results(self, results: list[ToolResult]) -> None:
        # All results of one assistant turn go back in a single user message.
        self.messages.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": r.call_id, "content": r.content, "is_error": r.is_error}
                for r in results
            ],
        })


class AnthropicProvider:
    def start(self, *, model: str, system: str, history: list[dict[str, str]], user_message: str,
              tools: list[dict[str, Any]]) -> AnthropicSession:
        return AnthropicSession(model, system, history, user_message, tools)
