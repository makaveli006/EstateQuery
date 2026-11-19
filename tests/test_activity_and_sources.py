from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from estatequery.agent.loop import run_turn
from estatequery.agent.providers.base import StepDone, ToolCall, Usage
from estatequery.agent.tools import collect_sources, source_label
from estatequery.config import ModelSpec
from estatequery.ui.activity import Activity

from test_loop_and_limits import FakeProvider

SPEC = ModelSpec("fake:model", "fake", "model", "Fake")


# --------------------------------------------------------------------------- activity


def test_titles_follow_each_call_through_its_states() -> None:
    activity = Activity()
    call = activity.start("a", "Search DarGlobal pages")
    assert call is not None and call.title == "Search DarGlobal pages"  # running: bare label
    assert activity.title == "Research activity · 1 tool execution"

    activity.start("b", "List DarGlobal projects")
    assert activity.finish("a", ok=True).title == "✓ Search DarGlobal pages"
    assert activity.finish("b", ok=False).title == "✗ List DarGlobal projects (failed)"
    assert activity.title == "Research activity · 2 tool executions · 1 did not complete"
    assert activity.summary == "1 completed, 1 failed"


def test_repeated_calls_to_one_tool_are_each_kept() -> None:
    activity = Activity()
    for call_id in ("r1-0-x", "r1-1-x", "r2-0-x"):
        activity.start(call_id, "List DarGlobal projects")
        activity.finish(call_id, ok=True)
    assert activity.total == 3
    assert activity.title == "Research activity · 3 tool executions"
    assert [c.title for c in activity.calls.values()] == ["✓ List DarGlobal projects"] * 3


def test_duplicate_and_unknown_events_do_not_create_or_change_rows() -> None:
    activity = Activity()
    assert activity.start("a", "Open listing") is not None
    assert activity.start("a", "Open listing") is None        # the same start, delivered twice
    assert activity.finish("missing", ok=True) is None
    assert activity.finish("a", ok=True) is not None
    assert activity.finish("a", ok=False) is None              # a second finish cannot flip the result
    assert activity.total == 1 and activity.calls["a"].title == "✓ Open listing"


def test_interrupting_only_touches_calls_still_running() -> None:
    activity = Activity()
    activity.start("done", "Open listing")
    activity.finish("done", ok=True)
    activity.start("stuck", "Search Wasalt listings")
    assert activity.interrupt_running() == ["stuck"]
    assert activity.calls["stuck"].title == "✗ Search Wasalt listings (interrupted)"
    assert activity.calls["done"].title == "✓ Open listing"
    assert activity.interrupt_running() == []                  # nothing left running
    assert activity.summary == "1 completed, 1 interrupted"


# --------------------------------------------------------------------------- sources


def test_source_labels_are_short_and_never_a_raw_url() -> None:
    assert source_label("Trump Branded Residences Gulf | DarGlobal Portfolio", "https://x/a") == "Trump Branded Residences Gulf"
    assert source_label(None, "https://darglobal.co.uk/partners/paramount") == "Paramount"
    assert source_label("https://darglobal.co.uk/partners/paramount", "https://darglobal.co.uk/partners/paramount") == "Paramount"
    assert source_label(None, "https://wasalt.sa/en/property/sale/apartment-with-3-bedrooms-5841667") == "Apartment With 3 Bedrooms"
    assert source_label("", "https://www.darglobal.co.uk/") == "darglobal.co.uk"
    long = source_label("word " * 40, "https://x/y")
    assert len(long) <= 60 and long.endswith("…")


def test_collect_sources_keeps_urls_and_tidies_titles() -> None:
    result = {"passages": [
        {"title": "W Residences Dubai Downtown | DarGlobal Branded Living", "url": "https://darglobal.co.uk/w-residences"},
        {"title": None, "url": "https://darglobal.co.uk/partners/paramount"},
    ]}
    assert collect_sources("search_darglobal", result) == [
        {"title": "W Residences Dubai Downtown", "url": "https://darglobal.co.uk/w-residences"},
        {"title": "Paramount", "url": "https://darglobal.co.uk/partners/paramount"},
    ]


# --------------------------------------------------------------------------- loop events


def test_tool_events_pair_up_by_call_id_even_for_parallel_calls_to_one_tool(db_path: Path) -> None:
    same_tool_twice = [
        ToolCall(id="call_0", name="search_listings", args={"city": "Riyadh"}),
        ToolCall(id="call_0", name="search_listings", args={"city": "Jeddah"}),   # providers may reuse ids
        ToolCall(id="call_2", name="get_listing", args={"id": "not-a-number"}),   # this one fails
    ]
    provider = FakeProvider([
        StepDone(text="", tool_calls=same_tool_twice, usage=Usage(1, 1)),
        StepDone(text="Done.", usage=Usage(1, 1)),
    ])

    async def run() -> list[dict[str, Any]]:
        return [e async for e in run_turn(spec=SPEC, history=[], user_message="hi", db_path=db_path, provider=provider)]

    events = asyncio.run(run())
    starts = [e for e in events if e["type"] == "tool_start"]
    ends = [e for e in events if e["type"] == "tool_end"]
    assert len(starts) == len(ends) == 3
    assert len({e["call_id"] for e in starts}) == 3                      # unique despite the reused id
    assert [e["call_id"] for e in starts] == [e["call_id"] for e in ends]
    assert [e["ok"] for e in ends] == [True, True, False]
    assert all({"tool", "args"} <= set(e) for e in starts)               # existing fields unchanged
    assert all({"tool", "summary"} <= set(e) for e in ends)
