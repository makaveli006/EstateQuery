from __future__ import annotations

import json
from pathlib import Path

import pytest

from estatequery.agent.loop import MAX_TOOL_RESULT_CHARS, summarize
from estatequery.agent.tools import LIST_BUDGET_CHARS, collect_sources, group_by_brand, run_tool
from estatequery.api.limits import Limiter
from estatequery.config import get_settings
from estatequery.ingest.build_db import build

BRANDS = ["Trump", "Pagani", "Missoni", "Elie Saab", "Aston Martin", None]


def make_db(tmp_path_factory: pytest.TempPathFactory, count: int, long_names: bool) -> Path:
    raw = tmp_path_factory.mktemp("raw")
    rows = []
    for i in range(count):
        name = f"Synthetic Residences Number {i:03d} by the Bay of Synthetic City" if long_names else f"Synthetic {i:02d}"
        slug = f"projects/synthetic-project-number-{i:03d}-with-a-long-address" if long_names else f"p/{i:02d}"
        rows.append({
            "url": f"https://example.test/{slug}",
            "name": name,
            "country": ["Oman", "UAE", "Qatar"][i % 3],
            "city": "Synthetic City Centre",
            "property_types": ["Apartments", "Villas"],
            "brand_partner": BRANDS[i % len(BRANDS)],
            "status": "Under development, completion scheduled for December 2029",
            "starting_price": "USD 1,000,000",
            "summary": "Synthetic.",
        })
    (raw / "darglobal_projects.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    path = tmp_path_factory.mktemp("db") / "estatequery.db"
    build(raw, path, snapshot="2026-11-19")
    return path


@pytest.fixture(scope="module")
def big_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """60 projects: too long to list in full, but short enough for the compact view to hold them all."""
    return make_db(tmp_path_factory, 60, long_names=False)


@pytest.fixture(scope="module")
def huge_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Many projects with long names, so even the compact view has to leave some out."""
    return make_db(tmp_path_factory, 60, long_names=True)


def test_a_long_project_list_comes_back_as_a_compact_view_with_every_project(big_db: Path) -> None:
    result = run_tool("list_darglobal_projects", {}, big_db)
    text = json.dumps(result, ensure_ascii=False)
    assert result["count"] == 60 and "by_brand" in result and "projects" not in result
    assert len(text) <= LIST_BUDGET_CHARS < MAX_TOOL_RESULT_CHARS      # never cut off by the agent loop

    items = [item for group in result["by_brand"].values() for item in group]
    assert len(items) == 60 and len({item["url"] for item in items}) == 60   # all of them, each once
    assert all({"name", "url"} <= set(item) for item in items)
    assert "omitted" not in result
    assert "note" in result and "get_darglobal_project" in result["note"]


def test_when_even_the_compact_view_is_too_long_projects_are_left_out_openly(huge_db: Path) -> None:
    result = run_tool("list_darglobal_projects", {}, huge_db)
    text = json.dumps(result, ensure_ascii=False)
    assert len(text) <= LIST_BUDGET_CHARS                              # still valid JSON under the cap
    shown = sum(len(group) for group in result["by_brand"].values())
    assert 0 < result["omitted"] and shown + result["omitted"] == 60  # nothing silently missing
    assert str(result["omitted"]) in result["note"] and "filter" in result["note"]
    json.loads(text)


def test_brands_are_alphabetical_with_unbranded_projects_last(big_db: Path) -> None:
    groups = list(run_tool("list_darglobal_projects", {}, big_db)["by_brand"])
    assert groups == ["Aston Martin", "Elie Saab", "Missoni", "Pagani", "Trump", "(no partner)"]


def test_a_filtered_list_that_fits_is_returned_unchanged(big_db: Path, db_path: Path) -> None:
    filtered = run_tool("list_darglobal_projects", {"brand": "pagani"}, big_db)
    assert "by_brand" not in filtered and filtered["count"] == 10
    assert {"name", "country", "city", "property_types", "brand_partner", "status", "slug", "url"} <= set(filtered["projects"][0])
    # the small fixture database used elsewhere keeps its original shape too
    small = run_tool("list_darglobal_projects", {}, db_path)
    assert "projects" in small and "by_brand" not in small


def test_sources_and_row_summary_understand_the_compact_view(big_db: Path) -> None:
    result = run_tool("list_darglobal_projects", {}, big_db)
    assert len(collect_sources("list_darglobal_projects", result)) == 60
    assert summarize(result) == "60 projects"


def test_group_by_brand_keeps_only_name_country_and_link() -> None:
    grouped = group_by_brand([
        {"name": "A", "country": "Oman", "brand_partner": "Trump", "url": "u1", "status": "x", "slug": "s"},
        {"name": "B", "brand_partner": None, "url": "u2"},
    ])
    assert grouped == {"Trump": [{"name": "A", "country": "Oman", "url": "u1"}],
                       "(no partner)": [{"name": "B", "url": "u2"}]}


def test_daily_claude_cap_is_100_and_the_message_no_longer_points_to_another_model() -> None:
    assert get_settings().daily_cap_anthropic == 100
    limiter = Limiter(per_minute=100, per_day=100, provider_caps={"anthropic": 2})
    assert limiter.check("a", "anthropic", now=0).allowed and limiter.check("b", "anthropic", now=0).allowed
    blocked = limiter.check("c", "anthropic", now=0)
    assert not blocked.allowed
    assert "tomorrow" in blocked.reason.lower() and "another model" not in blocked.reason.lower()
