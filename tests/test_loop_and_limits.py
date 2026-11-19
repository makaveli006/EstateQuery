from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator

from estatequery.agent.loop import run_turn, trim_history
from estatequery.agent.providers.base import StepDone, TextDelta, ToolCall, ToolResult, Usage
from estatequery.api.limits import Limiter
from estatequery.config import ModelSpec, get_settings

SPEC = ModelSpec("fake:model", "fake", "model", "Fake")


class FakeSession:
    """Scripted model: returns the queued steps in order and records what it was sent."""

    def __init__(self, script: list[StepDone]) -> None:
        self.script = list(script)
        self.allow_tools_seen: list[bool] = []
        self.results: list[list[ToolResult]] = []

    async def step(self, *, allow_tools: bool) -> AsyncIterator[TextDelta | StepDone]:
        self.allow_tools_seen.append(allow_tools)
        done = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if not allow_tools:  # a real provider cannot return tool calls here
            done = StepDone(text="final answer", usage=Usage(1, 1))
        if done.text:
            yield TextDelta(done.text)
        yield done

    def add_tool_results(self, results: list[ToolResult]) -> None:
        self.results.append(results)


class FakeProvider:
    def __init__(self, script: list[StepDone]) -> None:
        self.session = FakeSession(script)

    def start(self, **_: Any) -> FakeSession:
        return self.session


def collect(provider: FakeProvider, db_path: Path) -> list[dict[str, Any]]:
    async def run() -> list[dict[str, Any]]:
        return [ev async for ev in run_turn(spec=SPEC, history=[], user_message="hi", db_path=db_path,
                                            provider=provider)]

    return asyncio.run(run())


def search_call(i: int = 0) -> ToolCall:
    return ToolCall(id=f"c{i}", name="search_listings", args={"city": "Riyadh", "purpose": "sale"})


def test_tool_round_then_answer(db_path: Path) -> None:
    provider = FakeProvider([
        StepDone(text="", tool_calls=[search_call()], usage=Usage(10, 2)),
        StepDone(text="Here are 3 listings.", usage=Usage(20, 5)),
    ])
    events = collect(provider, db_path)
    types = [e["type"] for i, e in enumerate(events) if not (e["type"] == "delta" and events[i - 1]["type"] == "delta")]
    assert types == ["tool_start", "tool_end", "delta", "sources", "listings", "done"]   # text may arrive in pieces
    assert "".join(e["text"] for e in events if e["type"] == "delta") == "Here are 3 listings."
    done = events[-1]
    assert done["answer"] == "Here are 3 listings."
    assert done["tool_rounds"] == 1 and done["tools_used"] == ["search_listings"]
    assert done["input_tokens"] == 30 and done["output_tokens"] == 7
    assert len(next(e for e in events if e["type"] == "sources")["items"]) == 3  # one source per matching listing
    assert '"total_matches": 3' in provider.session.results[0][0].content


def test_loop_stops_after_max_tool_rounds(db_path: Path) -> None:
    # A model that would call tools forever is cut off and forced to answer.
    provider = FakeProvider([StepDone(text="", tool_calls=[search_call()], usage=Usage(1, 1))])
    events = collect(provider, db_path)
    max_rounds = get_settings().max_tool_rounds
    assert provider.session.allow_tools_seen == [True] * max_rounds + [False]
    assert events[-1]["type"] == "done"
    assert events[-1]["tool_rounds"] == max_rounds
    assert events[-1]["answer"] == "final answer"


def test_malformed_tool_arguments_are_returned_as_an_error(db_path: Path) -> None:
    provider = FakeProvider([
        StepDone(text="", tool_calls=[ToolCall(id="c0", name="search_listings", args=None, raw_args="{bad")]),
        StepDone(text="Sorry, retried.", usage=Usage(1, 1)),
    ])
    events = collect(provider, db_path)
    result = provider.session.results[0][0]
    assert result.is_error and "not valid JSON" in result.content
    assert events[-1]["type"] == "done"


def test_provider_failure_becomes_an_error_event(db_path: Path) -> None:
    class Broken:
        def start(self, **_: Any) -> Any:
            raise RuntimeError("boom")

    events = collect(Broken(), db_path)  # type: ignore[arg-type]
    assert [e["type"] for e in events] == ["error"]
    assert "boom" not in events[0]["message"]  # internals are not leaked to the user


def test_trim_history() -> None:
    history = [{"role": "assistant", "content": "a"}, {"role": "user", "content": "b"},
               {"role": "system", "content": "ignore me"}, {"role": "assistant", "content": "c"}]
    assert trim_history(history, 10) == [{"role": "user", "content": "b"}, {"role": "assistant", "content": "c"}]
    assert trim_history(history, 1) == []


def test_limiter_per_minute_per_day_and_provider_cap() -> None:
    limiter = Limiter(per_minute=2, per_day=3, provider_caps={"anthropic": 2})
    assert limiter.check("1.1.1.1", "openrouter", now=0).allowed
    assert limiter.check("1.1.1.1", "openrouter", now=1).allowed
    blocked = limiter.check("1.1.1.1", "openrouter", now=2)
    assert not blocked.allowed and blocked.retry_after > 0
    assert limiter.check("1.1.1.1", "openrouter", now=61).allowed       # minute window moved on
    assert not limiter.check("1.1.1.1", "openrouter", now=200).allowed  # daily limit of 3 reached
    assert limiter.check("2.2.2.2", "anthropic", now=0).allowed
    assert limiter.check("3.3.3.3", "anthropic", now=0).allowed
    assert not limiter.check("4.4.4.4", "anthropic", now=0).allowed     # provider budget spent
    assert limiter.check("4.4.4.4", "openrouter", now=0).allowed
