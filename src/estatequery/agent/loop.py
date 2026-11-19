"""The agent loop: model call -> tool calls -> model call, the same for every provider.

`run_turn` is an async generator of plain dict events, which the API layer forwards to
the browser as server-sent events:

  {"type": "delta", "text": ...}                     streamed answer text
  {"type": "tool_start", "call_id": ..., "tool": ..., "args": ...}        a tool call began
  {"type": "tool_end", "call_id": ..., "tool": ..., "summary": ..., "ok": bool}  it finished

`call_id` is unique within a turn, so a consumer can pair each start with its finish even
when the same tool runs several times in parallel.
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
from estatequery.agent.guard import GuardPolicy, StreamGuard, sanitize
from estatequery.agent.prompts import system_prompt_and_info
from estatequery.agent.providers import ProviderError, get_provider
from estatequery.agent.providers.breaker import BREAKER, CircuitBreaker, fallback_specs
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
    if "by_brand" in result:
        return f"{result.get('count', 0)} projects"
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


def call_key(call: ToolCall) -> str | None:
    """Identity of a call: the tool and its arguments, whatever order they were written in."""
    if call.args is None:
        return None
    return f"{call.name}:{json.dumps(call.args, sort_keys=True, ensure_ascii=False, default=str)}"


def _pack(call: ToolCall, result: dict[str, Any]) -> ToolResult:
    text = json.dumps(result, ensure_ascii=False)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + ' ..."truncated"'
    return ToolResult(call_id=call.id, name=call.name, content=text, is_error="error" in result)


async def _execute(call: ToolCall, db_path: Path | None) -> tuple[ToolResult, dict[str, Any]]:
    if call.args is None:
        result: dict[str, Any] = {
            "error": "Tool arguments were not valid JSON. Call the tool again with a valid JSON object."}
    else:
        result = await asyncio.to_thread(run_tool, call.name, call.args, db_path)
    return _pack(call, result), result


def _reuse_or_refuse(call: ToolCall, action: str, earlier: dict[str, Any] | None) -> ToolResult:
    """The result for a call that did not run: the identical earlier result, or a refusal."""
    if action == "reuse" and earlier is not None:
        return _pack(call, {"note": "You already made this exact call in this conversation turn. "
                                    "Its result is repeated here; do not call it again.", **earlier})
    return _pack(call, {"error": "The tool-call budget for this question is used up. Answer with the "
                                 "information you already have."})


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
    fallbacks: list[tuple[ModelSpec, Provider]] | None = None,
    breaker: CircuitBreaker | None = None,
) -> AsyncIterator[dict[str, Any]]:
    settings = get_settings()
    breaker = breaker or BREAKER
    # The model asked for, then the fallbacks in order. A model whose circuit is open is skipped.
    chain: list[tuple[ModelSpec, Provider | None]] = [(spec, provider)] + (
        fallbacks if fallbacks is not None else [(m, None) for m in fallback_specs(spec, settings.fallback_models)])
    position = 0
    while position + 1 < len(chain) and breaker.is_open(chain[position][0].id):
        position += 1
    active, active_provider = chain[position]
    started = time.monotonic()
    usage = Usage()
    sources: dict[str, dict[str, str]] = {}
    source_labels: set[str] = set()
    listings: dict[Any, dict[str, Any]] = {}
    tools_used: list[str] = []
    cache: dict[str, dict[str, Any]] = {}      # raw result of every call that ran, by call_key
    duplicate_calls = 0                        # identical repeats answered from the cache, not re-run
    suppressed_calls = 0                       # calls refused because the budget was spent
    guard_on = settings.output_guard
    policy = GuardPolicy(allowed_domains=tuple(
        d.strip().lower() for d in settings.allowed_link_domains.split(",") if d.strip()))
    guard_removed: list[str] = []              # what the output guard took out of the answer
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

    system_text, prompt_info = system_prompt_and_info(db_path)

    def begin_session() -> Any:
        return (active_provider or get_provider(active)).start(
            model=active.api_model,
            system=system_text,
            history=trim_history(history, settings.max_history_messages),
            user_message=user_message,
            tools=TOOL_SPECS,
        )

    try:
        session = begin_session()
        if position:
            yield {"type": "fallback", "from": spec.id, "to": active.id, "reason": "circuit open"}

        while True:
            allow_tools = rounds < settings.max_tool_rounds
            generation = trace.generation(name=f"llm-call-{rounds + 1}", model=active.api_model,
                                          input={"round": rounds + 1, "tools_allowed": allow_tools},
                                          prompt=prompt_info.client)
            done: StepDone | None = None
            stream_guard = StreamGuard(policy) if guard_on else None
            shown_text = False
            try:
                async for event in session.step(allow_tools=allow_tools):
                    if isinstance(event, TextDelta):
                        text = stream_guard.feed(event.text) if stream_guard else event.text
                        if text:
                            shown_text = True
                            yield {"type": "delta", "text": text}
                    else:
                        done = event
                if stream_guard and (tail := stream_guard.flush()):
                    yield {"type": "delta", "text": tail}
            except ProviderError as exc:
                generation.end(level="ERROR", status_message=str(exc))
                if exc.retryable:
                    breaker.record_failure(active.id)
                # Switch models only while the user has seen nothing: the first call of the turn,
                # before any text or tool row. Later failures are reported as they are.
                if exc.retryable and rounds == 0 and not shown_text and position + 1 < len(chain):
                    failed = active
                    position += 1
                    while position + 1 < len(chain) and breaker.is_open(chain[position][0].id):
                        position += 1
                    active, active_provider = chain[position]
                    session = begin_session()
                    log.warning("falling back from %s to %s: %s", failed.id, active.id, exc)
                    yield {"type": "fallback", "from": failed.id, "to": active.id, "reason": str(exc)}
                    continue
                raise
            if done is None:
                generation.end(level="ERROR", status_message="no response")
                raise ProviderError("The model returned no response.")

            breaker.record_success(active.id)
            usage.add(done.usage)
            served_by = done.model or served_by
            generation.end(
                output={"text": done.text, "tool_calls": [{"name": c.name, "args": c.args} for c in done.tool_calls]},
                usage_details={"input": done.usage.input_tokens, "output": done.usage.output_tokens},
                metadata={"stop_reason": done.stop_reason, "served_by": done.model},
            )

            if not done.tool_calls or not allow_tools:
                answer = done.text
                if guard_on:
                    answer, guard_removed = sanitize(answer, policy)
                break

            rounds += 1
            # Provider call ids can repeat across rounds, so qualify them with the round.
            call_ids = [f"r{rounds}-{index}-{call.id}" for index, call in enumerate(done.tool_calls)]

            # Decide what each call needs: run it, reuse an identical earlier result, or refuse it
            # because the turn's tool-call budget is spent. Only calls that run are shown and counted.
            plan, running = [], {}
            budget_left = settings.max_tool_calls - len(tools_used)
            for index, call in enumerate(done.tool_calls):
                key = call_key(call)
                if key is not None and (key in cache or key in running):
                    plan.append(("reuse", key))
                    duplicate_calls += 1
                elif budget_left <= 0:
                    plan.append(("skip", key))
                    suppressed_calls += 1
                else:
                    plan.append(("run", key))
                    budget_left -= 1
                    if key is not None:
                        running[key] = index
            for call, call_id, (action, _) in zip(done.tool_calls, call_ids, plan):
                if action == "run":
                    yield {"type": "tool_start", "call_id": call_id, "tool": call.name, "args": call.args or {}}
            to_run = [c for c, (action, _) in zip(done.tool_calls, plan) if action == "run"]
            fresh = iter(await asyncio.gather(*(_execute(c, db_path) for c in to_run)))

            results: list[ToolResult] = []
            for call, call_id, (action, key) in zip(done.tool_calls, call_ids, plan):
                if action != "run":
                    results.append(_reuse_or_refuse(call, action, cache.get(key) if key else None))
                    trace.tool(name=call.name, args=call.args).end(
                        output={"note": action}, level="WARNING", metadata={"suppressed": action})
                    continue
                tool_result, raw = next(fresh)
                span = trace.tool(name=call.name, args=call.args)
                results.append(tool_result)
                tools_used.append(call.name)
                if key is not None:
                    cache[key] = raw
                span.end(output=raw, level="WARNING" if tool_result.is_error else None)
                policy.trust_result(call.name, raw)
                for src in collect_sources(call.name, raw):
                    policy.trust_url(src["url"])
                    # One entry per page, and one per label: two links that read the same
                    # are noise in the sources line.
                    if src["url"] not in sources and src["title"].lower() not in source_labels:
                        sources[src["url"]] = src
                        source_labels.add(src["title"].lower())
                for item in raw.get("listings") or []:
                    listings[item.get("id")] = item
                ended: dict[str, Any] = {"type": "tool_end", "call_id": call_id, "tool": call.name,
                                         "summary": summarize(raw), "ok": not tool_result.is_error}
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
                                              "latency_ms": latency_ms, "duplicate_calls": duplicate_calls,
                                              "suppressed_calls": suppressed_calls,
                                              "guard_removed": guard_removed,
                                              "served_model": active.id, "prompt_version": prompt_info.version,
                                              "fallback_from": spec.id if active is not spec else None})
        yield {
            "type": "done",
            "answer": answer,
            "model": active.id,
            "fallback_from": spec.id if active is not spec else None,
            "prompt_version": prompt_info.version,
            "served_by": served_by,
            "provider": active.provider,
            "latency_ms": latency_ms,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "tool_rounds": rounds,
            "tools_used": tools_used,
            "duplicate_calls": duplicate_calls,
            "suppressed_calls": suppressed_calls,
            "guard_removed": guard_removed,
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
