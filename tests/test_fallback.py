"""Model fallback and the circuit breaker."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
from test_loop_and_limits import FakeProvider, SPEC, search_call

from estatequery.agent.loop import run_turn
from estatequery.agent.providers.base import ProviderError, StepDone, TextDelta, Usage
from estatequery.agent.providers.breaker import CircuitBreaker, fallback_specs
from estatequery.config import MODELS_BY_ID, ModelSpec, get_settings

BACKUP = ModelSpec("other:backup", "other", "backup", "Backup model")


class FailingProvider:
    """A model that fails as soon as it is asked, the way a rate limit or an outage does."""

    def __init__(self, *, retryable: bool = True, text_first: str = "", fail_on_round: int = 1) -> None:
        self.retryable, self.text_first, self.fail_on_round = retryable, text_first, fail_on_round
        self.starts = 0
        self.steps = 0

    def start(self, **_: Any) -> "FailingProvider":
        self.starts += 1
        return self

    async def step(self, *, allow_tools: bool) -> AsyncIterator[Any]:
        self.steps += 1
        if self.steps < self.fail_on_round:       # earlier rounds succeed with a tool call
            yield StepDone(text="", tool_calls=[search_call(self.steps)], usage=Usage(1, 1))
            return
        if self.text_first:
            yield TextDelta(self.text_first)
        raise ProviderError("the provider failed", retryable=self.retryable)

    def add_tool_results(self, results: Any) -> None:
        pass


def good(text: str = "Answer from the backup.") -> FakeProvider:
    return FakeProvider([StepDone(text=text, usage=Usage(1, 1))])


def run(provider: Any, fallbacks: list, db_path: Path, breaker: CircuitBreaker | None = None) -> list[dict[str, Any]]:
    async def go() -> list[dict[str, Any]]:
        return [ev async for ev in run_turn(spec=SPEC, history=[], user_message="hi", db_path=db_path,
                                            provider=provider, fallbacks=fallbacks, breaker=breaker or CircuitBreaker())]

    return asyncio.run(go())


def types(events: list[dict[str, Any]]) -> list[str]:
    return [e["type"] for e in events]


def test_a_retryable_failure_at_the_start_switches_to_the_fallback(db_path: Path) -> None:
    primary, backup = FailingProvider(), good()
    events = run(primary, [(BACKUP, backup)], db_path)
    assert types(events)[0] == "fallback" and events[0]["from"] == SPEC.id and events[0]["to"] == BACKUP.id
    done = events[-1]
    assert done["type"] == "done" and done["answer"] == "Answer from the backup."
    assert done["model"] == BACKUP.id and done["fallback_from"] == SPEC.id and done["provider"] == "other"
    assert primary.starts == 1


def test_no_fallback_event_or_marker_when_the_primary_works(db_path: Path) -> None:
    events = run(good("Fine."), [(BACKUP, good())], db_path)
    assert "fallback" not in types(events)
    assert events[-1]["model"] == SPEC.id and events[-1]["fallback_from"] is None


def test_a_non_retryable_failure_is_reported_not_retried_elsewhere(db_path: Path) -> None:
    backup = good()
    events = run(FailingProvider(retryable=False), [(BACKUP, backup)], db_path)
    assert types(events) == ["error"] and events[0]["retryable"] is False
    assert backup.session.allow_tools_seen == []              # the backup was never asked


def test_nothing_is_switched_once_the_user_has_seen_text(db_path: Path) -> None:
    backup = good()
    events = run(FailingProvider(text_first="Here are the listings that "), [(BACKUP, backup)], db_path)
    assert "fallback" not in types(events) and types(events)[-1] == "error"
    assert backup.session.allow_tools_seen == []


def test_nothing_is_switched_after_a_tool_round(db_path: Path) -> None:
    backup = good()
    events = run(FailingProvider(fail_on_round=2), [(BACKUP, backup)], db_path)
    assert types(events).count("tool_start") == 1 and "fallback" not in types(events)
    assert types(events)[-1] == "error" and backup.session.allow_tools_seen == []


def test_without_fallbacks_the_error_is_reported(db_path: Path) -> None:
    events = run(FailingProvider(), [], db_path)
    assert types(events) == ["error"] and events[0]["retryable"] is True


def test_when_the_fallback_fails_too_the_user_gets_one_error(db_path: Path) -> None:
    events = run(FailingProvider(), [(BACKUP, FailingProvider())], db_path)
    assert types(events) == ["fallback", "error"]


def test_a_chain_of_several_fallbacks_is_walked_in_order(db_path: Path) -> None:
    third = ModelSpec("other:third", "other", "third", "Third")
    events = run(FailingProvider(), [(BACKUP, FailingProvider()), (third, good("Third."))], db_path)
    assert [e["to"] for e in events if e["type"] == "fallback"] == [BACKUP.id, third.id]
    assert events[-1]["model"] == third.id


# ---------------------------------------------------------------------------- circuit breaker

class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_the_circuit_opens_after_repeated_failures_and_closes_after_the_cooldown() -> None:
    clock = Clock()
    breaker = CircuitBreaker(threshold=3, window=60, cooldown=30, clock=clock)
    breaker.record_failure("m")
    breaker.record_failure("m")
    assert not breaker.is_open("m")
    breaker.record_failure("m")
    assert breaker.is_open("m")
    clock.now = 29
    assert breaker.is_open("m")
    clock.now = 31
    assert not breaker.is_open("m")                 # cooldown over: one request may try
    breaker.record_success("m")
    breaker.record_failure("m")
    assert not breaker.is_open("m")                 # a success cleared the history


def test_failures_outside_the_window_do_not_count() -> None:
    clock = Clock()
    breaker = CircuitBreaker(threshold=3, window=60, cooldown=30, clock=clock)
    for at in (0, 40, 100):
        clock.now = at
        breaker.record_failure("m")
    assert not breaker.is_open("m")


def test_a_failed_trial_request_reopens_the_circuit_at_once() -> None:
    clock = Clock()
    breaker = CircuitBreaker(threshold=2, window=60, cooldown=30, clock=clock)
    breaker.record_failure("m"); breaker.record_failure("m")
    assert breaker.is_open("m")
    clock.now = 31
    assert not breaker.is_open("m")                 # trial allowed
    breaker.record_failure("m")                     # the trial failed
    assert breaker.is_open("m")


def test_circuits_are_kept_per_model() -> None:
    breaker = CircuitBreaker(threshold=1)
    breaker.record_failure("a")
    assert breaker.is_open("a") and not breaker.is_open("b")


def test_an_open_circuit_sends_requests_straight_to_the_fallback(db_path: Path) -> None:
    clock = Clock()
    breaker = CircuitBreaker(threshold=2, window=60, cooldown=30, clock=clock)
    primary = FailingProvider()
    for _ in range(2):                              # two slow failures open the circuit
        run(primary, [(BACKUP, good())], db_path, breaker)
    assert primary.starts == 2 and breaker.is_open(SPEC.id)

    events = run(primary, [(BACKUP, good())], db_path, breaker)
    assert primary.starts == 2                      # not asked again
    assert events[0]["type"] == "fallback" and events[0]["reason"] == "circuit open"
    assert events[-1]["model"] == BACKUP.id

    clock.now = 31                                  # cooldown over: the primary is tried again and recovers
    healthy = good("Primary is back.")
    events = run(healthy, [(BACKUP, good())], db_path, breaker)
    assert events[-1]["model"] == SPEC.id and not breaker.is_open(SPEC.id)


def test_the_last_model_is_still_tried_when_every_circuit_is_open(db_path: Path) -> None:
    breaker = CircuitBreaker(threshold=1, cooldown=1000)
    breaker.record_failure(SPEC.id)
    breaker.record_failure(BACKUP.id)
    events = run(good("Only option."), [(BACKUP, good("Backup."))], db_path, breaker)
    assert events[-1]["type"] == "done"             # never stranded with nothing to call


# ---------------------------------------------------------------------------- which models are fallbacks

def test_fallback_specs_use_known_models_with_a_configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "anthropic_api_key", "test")
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    sonnet = MODELS_BY_ID["anthropic:claude-sonnet-5-5"]
    csv = "anthropic:claude-haiku-5-5, anthropic:claude-sonnet-5-5, nope:model, openrouter:google/gemini-3.8-flash, anthropic:claude-haiku-5-5"
    assert [m.id for m in fallback_specs(sonnet, csv)] == ["anthropic:claude-haiku-5-5"]   # self, unknown, no key, duplicate: out
    monkeypatch.setattr(settings, "openrouter_api_key", "test")
    assert [m.id for m in fallback_specs(sonnet, csv)] == [
        "anthropic:claude-haiku-5-5", "openrouter:google/gemini-3.8-flash"]               # another provider works too
    assert fallback_specs(sonnet, "") == []
