"""OpenRouter adapter (OpenAI-compatible chat completions with streaming tool calls)."""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any, AsyncIterator

import openai
from openai import AsyncOpenAI

from estatequery.agent.providers.base import ProviderError, StepDone, TextDelta, ToolCall, ToolResult, Usage
from estatequery.config import get_settings

log = logging.getLogger("estatequery.providers.openrouter")

BASE_URL = "https://openrouter.ai/api/v1"
MAX_OUTPUT_TOKENS = 3000


@lru_cache
def _client() -> AsyncOpenAI:
    settings = get_settings()
    if not settings.openrouter_api_key:
        raise ProviderError("OPENROUTER_API_KEY is not configured.")
    return AsyncOpenAI(
        base_url=BASE_URL,
        api_key=settings.openrouter_api_key,
        timeout=60.0,
        max_retries=1,
        default_headers={"X-Title": "EstateQuery"},
    )


def _to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
        for t in tools
    ]


class OpenRouterSession:
    def __init__(self, model: str, system: str, history: list[dict[str, str]], user_message: str,
                 tools: list[dict[str, Any]]) -> None:
        self.model = model
        self.tools = _to_openai_tools(tools)
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        self.messages += [{"role": m["role"], "content": m["content"]} for m in history]
        self.messages.append({"role": "user", "content": user_message})

    async def step(self, *, allow_tools: bool) -> AsyncIterator[TextDelta | StepDone]:
        text_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        usage = Usage()
        stop_reason: str | None = None
        served_by: str | None = None
        try:
            stream = await _client().chat.completions.create(
                model=self.model,
                messages=self.messages,
                tools=self.tools,
                tool_choice="auto" if allow_tools else "none",
                max_tokens=MAX_OUTPUT_TOKENS,
                # No sampling parameters: several current models reject `temperature`.
                stream=True,
                stream_options={"include_usage": True},
                # Only route to hosts that honour every parameter we send (tools included).
                extra_body={"provider": {"require_parameters": True}},
            )
            async for chunk in stream:
                served_by = getattr(chunk, "model", None) or served_by
                if getattr(chunk, "usage", None):
                    usage = Usage(chunk.usage.prompt_tokens or 0, chunk.usage.completion_tokens or 0)
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if choice.finish_reason:
                    stop_reason = choice.finish_reason
                delta = choice.delta
                if delta is None:
                    continue
                if delta.content:
                    text_parts.append(delta.content)
                    yield TextDelta(delta.content)
                for tc in delta.tool_calls or []:
                    slot = calls.setdefault(tc.index or 0, {"id": "", "name": "", "arguments": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function and tc.function.name:
                        slot["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        slot["arguments"] += tc.function.arguments
        except openai.RateLimitError as exc:
            raise ProviderError("The model is rate limited right now. Try again shortly or pick another model.",
                                retryable=True) from exc
        except openai.AuthenticationError as exc:
            raise ProviderError("OpenRouter rejected the API key.") from exc
        except openai.APIStatusError as exc:
            log.warning("OpenRouter API error %s for %s: %s", exc.status_code, self.model, str(exc)[:500])
            raise ProviderError(f"OpenRouter returned an error ({exc.status_code}) for {self.model}. "
                                "Pick another model.", retryable=exc.status_code >= 500) from exc
        except openai.APIConnectionError as exc:
            raise ProviderError("Could not reach OpenRouter.", retryable=True) from exc

        text = "".join(text_parts)
        tool_calls: list[ToolCall] = []
        wire_calls: list[dict[str, Any]] = []
        for index in sorted(calls):
            slot = calls[index]
            if not slot["name"]:
                continue
            call_id = slot["id"] or f"call_{index}"
            raw = slot["arguments"] or "{}"
            try:
                args = json.loads(raw)
                if not isinstance(args, dict):
                    args = None
            except json.JSONDecodeError:
                args = None
            tool_calls.append(ToolCall(id=call_id, name=slot["name"], args=args, raw_args=raw))
            # Echo back valid JSON even when the model's arguments were malformed, so the
            # next request is accepted; the error itself travels in the tool result.
            wire_calls.append({"id": call_id, "type": "function",
                               "function": {"name": slot["name"], "arguments": raw if args is not None else "{}"}})

        assistant: dict[str, Any] = {"role": "assistant", "content": text or None}
        if wire_calls:
            assistant["tool_calls"] = wire_calls
        self.messages.append(assistant)
        yield StepDone(text=text, tool_calls=tool_calls, usage=usage, stop_reason=stop_reason, model=served_by)

    def add_tool_results(self, results: list[ToolResult]) -> None:
        for r in results:
            self.messages.append({"role": "tool", "tool_call_id": r.call_id, "content": r.content})


class OpenRouterProvider:
    def start(self, *, model: str, system: str, history: list[dict[str, str]], user_message: str,
              tools: list[dict[str, Any]]) -> OpenRouterSession:
        return OpenRouterSession(model, system, history, user_message, tools)
