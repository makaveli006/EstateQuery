from __future__ import annotations

import json
from pathlib import Path

from estatequery.agent.tools import TOOL_SPECS, collect_sources, run_tool
from estatequery.ingest.build_db import chunk_text


def test_search_filters_and_sort(db_path: Path) -> None:
    result = run_tool("search_listings", {"purpose": "sale", "city": "riyadh", "sort": "price_asc"}, db_path)
    assert result["total_matches"] == 3
    assert [x["id"] for x in result["listings"]] == [900002, 900001, 900003]
    assert result["source"] == "Wasalt.sa" and result["snapshot_date"] == "2026-11-19"


def test_search_is_tolerant_of_hyphens_and_case(db_path: Path) -> None:
    result = run_tool("search_listings", {"district": "al malqa", "purpose": "sale"}, db_path)
    assert result["total_matches"] == 2


def test_search_ranges_and_limit(db_path: Path) -> None:
    result = run_tool("search_listings", {"purpose": "sale", "min_bedrooms": 4, "max_price": 3000000}, db_path)
    assert [x["id"] for x in result["listings"]] == [900004]
    capped = run_tool("search_listings", {"limit": 50}, db_path)
    assert capped["returned"] <= 8


def test_search_keywords(db_path: Path) -> None:
    result = run_tool("search_listings", {"keywords": "swimming pool"}, db_path)
    assert [x["id"] for x in result["listings"]] == [900003]


def test_zero_results_return_valid_values(db_path: Path) -> None:
    result = run_tool("search_listings", {"city": "Riyadh", "district": "Nowhere"}, db_path)
    assert result["total_matches"] == 0
    assert "Riyadh" in result["available"]["cities"]
    assert "Al-Malqa" in result["available"]["districts_in_city"]


def test_stats_median_and_no_sale_rent_mixing(db_path: Path) -> None:
    result = run_tool("listing_stats", {"group_by": "city", "purpose": "sale"}, db_path)
    groups = {g["group"]: g for g in result["groups"]}
    assert groups["Riyadh"]["count"] == 3
    assert groups["Riyadh"]["median_price"] == 900000
    assert groups["Riyadh"]["median_price_per_sqm"] == 7000
    assert "Dammam" not in groups  # its only listing has no price
    assert result["total_listings"] == 4


def test_stats_requires_valid_group(db_path: Path) -> None:
    assert "error" in run_tool("listing_stats", {"group_by": "owner"}, db_path)


def test_get_listing(db_path: Path) -> None:
    record = run_tool("get_listing", {"id": 900001}, db_path)
    assert record["extras"] == {"age": "New", "facade": "North"}
    assert record["bedrooms"] == 3
    assert "error" in run_tool("get_listing", {"id": 1}, db_path)
    assert "error" in run_tool("get_listing", {"id": "abc"}, db_path)


def test_darglobal_tools(db_path: Path) -> None:
    listed = run_tool("list_darglobal_projects", {"country": "testland"}, db_path)
    assert listed["count"] == 1 and listed["projects"][0]["name"] == "Sample Tower"

    found = run_tool("search_darglobal", {"query": "what amenities, cinema?"}, db_path)
    assert found["returned"] >= 1
    assert "cinema" in found["passages"][0]["text"]
    assert found["passages"][0]["heading"] == "Amenities"

    project = run_tool("get_darglobal_project", {"project": "sample tower"}, db_path)
    assert project["facts"]["starting_price"] == "USD 500,000"
    assert "forty storey" in project["page"]["text"]
    assert "error" in run_tool("get_darglobal_project", {"project": "does-not-exist"}, db_path)


def test_unknown_tool_and_bad_args_do_not_raise(db_path: Path) -> None:
    assert "error" in run_tool("drop_tables", {}, db_path)
    assert "error" in run_tool("search_listings", "not a dict", db_path)  # type: ignore[arg-type]
    assert "error" in run_tool("search_listings", {"min_price": "cheap"}, db_path)


def test_sql_injection_is_inert(db_path: Path) -> None:
    result = run_tool("search_listings", {"city": "x'; DROP TABLE listings; --"}, db_path)
    assert result["total_matches"] == 0
    assert run_tool("search_listings", {}, db_path)["total_matches"] == 7


def test_collect_sources(db_path: Path) -> None:
    result = run_tool("search_listings", {"city": "Jeddah"}, db_path)
    sources = collect_sources("search_listings", result)
    assert {s["url"] for s in sources} == {
        "https://example.test/listing/900004", "https://example.test/listing/900006"}


def test_tool_specs_are_valid_json_schema_objects() -> None:
    names = [spec["name"] for spec in TOOL_SPECS]
    assert len(names) == len(set(names)) == 6
    for spec in TOOL_SPECS:
        assert spec["parameters"]["type"] == "object"
        assert set(spec["parameters"]["required"]) <= set(spec["parameters"]["properties"])
        json.dumps(spec)


def test_chunk_text_splits_on_headings_and_size() -> None:
    text = "Title line that is long enough to be kept as a passage on its own\nOverview\n" + ("word " * 300)
    chunks = chunk_text(text, ["Overview"], size=400)
    assert chunks[0][0] is None
    assert all(heading == "Overview" for heading, _ in chunks[1:])
    assert all(len(body) <= 420 for _, body in chunks)
    assert len(chunks) >= 4
