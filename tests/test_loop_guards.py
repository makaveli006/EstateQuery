"""Duplicate-call reuse and the per-question tool-call budget."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_loop_and_limits import FakeProvider, collect, search_call

from estatequery.agent.loop import call_key
from estatequery.agent.providers.base import StepDone, ToolCall, Usage
from estatequery.config import get_settings


def search(city: str, call_id: str, **extra) -> ToolCall:
    return ToolCall(id=call_id, name="search_listings", args={"purpose": "sale", "city": city, **extra})


def test_call_key_ignores_argument_order_and_rejects_unparseable_calls() -> None:
    a = ToolCall(id="1", name="search_listings", args={"city": "Riyadh", "purpose": "sale"})
    b = ToolCall(id="2", name="search_listings", args={"purpose": "sale", "city": "Riyadh"})
    assert call_key(a) == call_key(b)
    assert call_key(a) != call_key(ToolCall(id="3", name="listing_stats", args=a.args))
    assert call_key(ToolCall(id="4", name="search_listings", args=None)) is None


def test_an_identical_call_in_the_same_batch_runs_once_and_both_get_a_result(db_path: Path) -> None:
    provider = FakeProvider([
        StepDone(text="", tool_calls=[search_call(0), search_call(1)], usage=Usage(1, 1)),
        StepDone(text="Done.", usage=Usage(1, 1)),
    ])
    events = collect(provider, db_path)
    assert [e["type"] for e in events].count("tool_start") == 1       # one row in the research section
    done = events[-1]
    assert done["tools_used"] == ["search_listings"]
    assert done["duplicate_calls"] == 1 and done["suppressed_calls"] == 0
    first, second = provider.session.results[0]
    assert not first.is_error and "You already made this exact call" in second.content
    assert '"total_matches": 3' in second.content                       # the earlier result is repeated in full


def test_a_repeat_in_a_later_round_is_answered_from_the_first_result(db_path: Path) -> None:
    provider = FakeProvider([
        StepDone(text="", tool_calls=[search_call(0)], usage=Usage(1, 1)),
        StepDone(text="", tool_calls=[search_call(1)], usage=Usage(1, 1)),
        StepDone(text="Done.", usage=Usage(1, 1)),
    ])
    events = collect(provider, db_path)
    done = events[-1]
    assert done["tool_rounds"] == 2 and done["tools_used"] == ["search_listings"]
    assert done["duplicate_calls"] == 1
    assert "already made this exact call" in provider.session.results[1][0].content


def test_different_arguments_are_a_different_call(db_path: Path) -> None:
    provider = FakeProvider([
        StepDone(text="", tool_calls=[search("Riyadh", "a"), search("Jeddah", "b")], usage=Usage(1, 1)),
        StepDone(text="Done.", usage=Usage(1, 1)),
    ])
    done = collect(provider, db_path)[-1]
    assert done["tools_used"] == ["search_listings", "search_listings"] and done["duplicate_calls"] == 0


def test_calls_beyond_the_budget_are_refused_with_an_error_result(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "max_tool_calls", 2)
    provider = FakeProvider([
        StepDone(text="", tool_calls=[search("Riyadh", "a"), search("Jeddah", "b"), search("Makkah", "c")],
                 usage=Usage(1, 1)),
        StepDone(text="Done.", usage=Usage(1, 1)),
    ])
    events = collect(provider, db_path)
    done = events[-1]
    assert done["tools_used"] == ["search_listings", "search_listings"]
    assert done["suppressed_calls"] == 1 and done["duplicate_calls"] == 0
    results = provider.session.results[0]
    assert [r.is_error for r in results] == [False, False, True]
    assert "budget" in results[2].content
    assert [e["type"] for e in events].count("tool_start") == 2


def test_the_budget_counts_across_rounds_but_not_repeats(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "max_tool_calls", 2)
    provider = FakeProvider([
        StepDone(text="", tool_calls=[search("Riyadh", "a")], usage=Usage(1, 1)),
        StepDone(text="", tool_calls=[search("Riyadh", "b"), search("Jeddah", "c"), search("Makkah", "d")],
                 usage=Usage(1, 1)),
        StepDone(text="Done.", usage=Usage(1, 1)),
    ])
    done = collect(provider, db_path)[-1]
    assert done["tools_used"] == ["search_listings", "search_listings"]   # Riyadh, then Jeddah
    assert done["duplicate_calls"] == 1                                   # the Riyadh repeat did not use budget
    assert done["suppressed_calls"] == 1                                  # Makkah did not fit


def test_an_endless_repeating_model_is_still_cut_off(db_path: Path) -> None:
    provider = FakeProvider([StepDone(text="", tool_calls=[search_call()], usage=Usage(1, 1))])
    done = collect(provider, db_path)[-1]
    assert done["tool_rounds"] == get_settings().max_tool_rounds
    assert done["tools_used"] == ["search_listings"]                       # it ran once; the rest were repeats
    assert done["duplicate_calls"] == get_settings().max_tool_rounds - 1
