"""The agent loop: model call -> tool calls -> model call, the same for every provider.

`run_turn` is an async generator of plain dict events, which the API layer forwards to
the browser as server-sent events:

  {"type": "delta", "text": ...}                     streamed answer text
  {"type": "tool_start", "tool": ..., "args": ...}   a tool call began
  {"type": "tool_end", "tool": ..., "summary": ...}  it finished
  {"type": "sources", "items": [...]}                links used in the answer
  {"type": "listings", "items": [...]}               listing rows retrieved, for cards
  {"type": "done", ...}                              final metadata
  {"type": "error", "message": ...}
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, AsyncIterator

from estatequery import observability
from estatequery.agent.prompts import system_prompt
from estatequery.agent.providers import ProviderError, get_provider
from estatequery.agent.providers.base import Provider, StepDone, TextDelta, ToolCall, ToolResult, Usage
from estatequery.agent.tools import TOOL_SPECS, collect_sources, run_tool
from estatequery.config import ModelSpec, get_settings

log = logging.getLogger("estatequery.agent")

MAX_TOOL_RESULT_CHARS = 7000
MAX_SOURCES = 10


def summarize(result: dict[str, Any]) -> str:
    """One short phrase describing a tool result, shown next to the tool name in the UI."""
    if "error" in result:
        return "error"
    if "total_matches" in result:
        return f"{result['total_matches']} matches"
    if "groups" in result:
        return f"{len(result['groups'])} groups from {result.get('total_listings', 0)} listings"
    if "passages" in result:
        return f"{result.get('returned', 0)} passages"
    if "projects" in result:
        return f"{result.get('count', 0)} projects"
    return "found"


def trim_history(history: list[dict[str, str]], limit: int) -> list[dict[str, str]]:
    """Keep the most recent messages, starting on a user message, with valid roles only."""
    clean = [
        {"role": m["role"], "content": str(m["content"])[:6000]}
        for m in history
        if m.get("role") in ("user", "assistant") and m.get("content")
    ][-limit:]
    while clean and clean[0]["role"] != "user":
        clean.pop(0)
    return clean


async def _execute(call: ToolCall, db_path: Path | None) -> tuple[ToolResult, dict[str, Any]]:
    if call.args is None:
        result: dict[str, Any] = {
            "error": "Tool arguments were not valid JSON. Call the tool again with a valid JSON object."}
    else:
        result = await asyncio.to_thread(run_tool, call.name, call.args, db_path)
    text = json.dumps(result, ensure_ascii=False)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + ' ..."truncated"'
    return ToolResult(call_id=call.id, name=call.name, content=text, is_error="error" in result), result


async def run_turn(
    *,
    spec: ModelSpec,
    history: list[dict[str, str]],
    user_message: str,
    session_id: str | None = None,
    db_path: Path | None = None,
    provider: Provider | None = None,
    tags: list[str] | None = None,
    emit_tool_results: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    settings = get_settings()
    started = time.monotonic()
    usage = Usage()
    sources: dict[str, dict[str, str]] = {}
    listings: dict[Any, dict[str, Any]] = {}
    tools_used: list[str] = []
    answer = ""
    rounds = 0
    served_by: str | None = None

    trace = observability.TurnTrace(
        name="chat-turn",
        user_input=user_message,
        session_id=session_id,
        tags=[spec.provider, spec.api_model, *(tags or [])],
        metadata={"provider": spec.provider, "model": spec.api_model},
    )

    try:
        provider = provider or get_provider(spec)
        session = provider.start(
            model=spec.api_model,
            system=system_prompt(db_path),
            history=trim_history(history, settings.max_history_messages),
            user_message=user_message,
            tools=TOOL_SPECS,
        )

        while True:
            allow_tools = rounds < settings.max_tool_rounds
            generation = trace.generation(name=f"llm-call-{rounds + 1}", model=spec.api_model,
                                          input={"round": rounds + 1, "tools_allowed": allow_tools})
            done: StepDone | None = None
            try:
                async for event in session.step(allow_tools=allow_tools):
                    if isinstance(event, TextDelta):
                        yield {"type": "delta", "text": event.text}
                    else:
                        done = event
            except ProviderError as exc:
                generation.end(level="ERROR", status_message=str(exc))
                raise
            if done is None:
                generation.end(level="ERROR", status_message="no response")
                raise ProviderError("The model returned no response.")

            usage.add(done.usage)
            served_by = done.model or served_by
            generation.end(
                output={"text": done.text, "tool_calls": [{"name": c.name, "args": c.args} for c in done.tool_calls]},
                usage_details={"input": done.usage.input_tokens, "output": done.usage.output_tokens},
                metadata={"stop_reason": done.stop_reason, "served_by": done.model},
            )

            if not done.tool_calls or not allow_tools:
                answer = done.text
                break

            rounds += 1
            for call in done.tool_calls:
                yield {"type": "tool_start", "tool": call.name, "args": call.args or {}}
            spans = [trace.tool(name=c.name, args=c.args) for c in done.tool_calls]
            executed = await asyncio.gather(*(_execute(c, db_path) for c in done.tool_calls))

            results: list[ToolResult] = []
            for call, span, (tool_result, raw) in zip(done.tool_calls, spans, executed):
                results.append(tool_result)
                tools_used.append(call.name)
                span.end(output=raw, level="WARNING" if tool_result.is_error else None)
                for src in collect_sources(call.name, raw):
                    sources.setdefault(src["url"], src)
                for item in raw.get("listings") or []:
                    listings[item.get("id")] = item
                ended: dict[str, Any] = {"type": "tool_end", "tool": call.name, "summary": summarize(raw)}
                if emit_tool_results:  # the evaluator needs the evidence the model saw
                    ended.update(args=call.args, result=raw)
                yield ended
            session.add_tool_results(results)

        if not answer.strip():
            answer = "I could not produce an answer for that. Please try rephrasing the question."
            yield {"type": "delta", "text": answer}

        latency_ms = int((time.monotonic() - started) * 1000)
        if sources:
            yield {"type": "sources", "items": list(sources.values())[:MAX_SOURCES]}
        if listings:
            yield {"type": "listings", "items": list(listings.values())[-8:]}
        trace.finish(output=answer, metadata={"tool_rounds": rounds, "tools_used": tools_used,
                                              "latency_ms": latency_ms})
        yield {
            "type": "done",
            "answer": answer,
            "model": spec.id,
            "served_by": served_by,
            "provider": spec.provider,
            "latency_ms": latency_ms,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "tool_rounds": rounds,
            "tools_used": tools_used,
            "trace_id": trace.trace_id,
        }
    except ProviderError as exc:
        trace.finish(output=None, error=str(exc))
        yield {"type": "error", "message": str(exc), "retryable": exc.retryable, "trace_id": trace.trace_id}
    except Exception as exc:  # noqa: BLE001 - last line of defence for the stream
        log.exception("chat turn failed")
        trace.finish(output=None, error=f"{type(exc).__name__}: {exc}")
        yield {"type": "error", "message": "Something went wrong while answering. Please try again.",
               "retryable": True, "trace_id": trace.trace_id}
