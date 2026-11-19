from __future__ import annotations

from pathlib import Path

import pytest

from estatequery.agent.tools import GROUPS, SORTS, TOOL_SPECS, run_tool
from estatequery.agent.validation import GROUP_VALUES, MODELS, SORT_VALUES, validate_args


def test_every_validator_lists_exactly_the_properties_the_model_is_shown() -> None:
    assert set(MODELS) == {t["name"] for t in TOOL_SPECS}
    for spec in TOOL_SPECS:
        shown = set(spec["parameters"]["properties"])
        assert set(MODELS[spec["name"]].model_fields) == shown, spec["name"]
        required = {name for name, f in MODELS[spec["name"]].model_fields.items() if f.is_required()}
        assert required == set(spec["parameters"].get("required", [])), spec["name"]


def test_enum_values_match_the_tools_own_tables() -> None:
    assert set(SORT_VALUES) == set(SORTS) and set(GROUP_VALUES) == set(GROUPS)
    schema = {t["name"]: t["parameters"]["properties"] for t in TOOL_SPECS}
    assert set(schema["search_listings"]["sort"]["enum"]) == set(SORT_VALUES)
    assert set(schema["listing_stats"]["group_by"]["enum"]) == set(GROUP_VALUES)


def test_a_valid_call_comes_back_cleaned() -> None:
    clean, problem = validate_args("search_listings", {
        "purpose": " Rent ", "city": " Riyadh ", "min_price": "1,500,000", "max_bedrooms": 4, "limit": "3"})
    assert problem is None
    assert clean == {"purpose": "rent", "city": "Riyadh", "min_price": 1500000.0, "max_bedrooms": 4.0, "limit": 3}


def test_limits_are_clamped_not_rejected() -> None:
    assert validate_args("search_listings", {"limit": 500})[0]["limit"] == 8
    assert validate_args("search_listings", {"limit": 0})[0]["limit"] == 1
    assert validate_args("search_listings", {})[0]["limit"] == 5
    assert validate_args("search_darglobal", {"query": "golf", "limit": 99})[0]["limit"] == 6


@pytest.mark.parametrize("name,args,arg,fragment", [
    ("search_listings", {"purpose": "lease"}, "purpose", "sale"),
    ("search_listings", {"min_price": "cheap"}, "min_price", ""),
    ("search_listings", {"min_price": -5}, "min_price", ""),
    ("search_listings", {"city": "Riyadh", "colour": "red"}, "colour", "not an argument"),
    ("search_listings", {"min_price": 900, "max_price": 100}, "arguments", "above"),
    ("search_listings", {"sort": "random"}, "sort", "price_asc"),
    ("listing_stats", {}, "group_by", "required"),
    ("listing_stats", {"group_by": "planet"}, "group_by", "city"),
    ("get_listing", {"id": "abc"}, "id", ""),
    ("get_listing", {"id": 0}, "id", ""),
    ("get_listing", {}, "id", "required"),
    ("search_darglobal", {"query": ""}, "query", ""),
    ("get_darglobal_project", {}, "project", "required"),
])
def test_bad_calls_name_the_invalid_argument_and_the_valid_ones(name, args, arg, fragment) -> None:
    clean, problem = validate_args(name, args)
    assert clean is None and problem is not None
    assert problem["error"].startswith(f"Invalid arguments for {name}")
    assert any(p["arg"] == arg and fragment in p["problem"] for p in problem["invalid"]), problem
    assert "valid_arguments" in problem and problem["valid_arguments"] == sorted(problem["valid_arguments"])


def test_text_is_length_capped() -> None:
    assert validate_args("get_darglobal_project", {"project": "x" * 500})[1] is not None


def test_numbers_that_arrive_as_text_become_ids() -> None:
    assert validate_args("get_listing", {"id": "5841667"})[0] == {"id": 5841667}
    assert validate_args("get_listing", {"id": 12.0})[0] == {"id": 12}


def test_unknown_tool_is_left_to_run_tool() -> None:
    assert validate_args("drop_tables", {"x": 1}) == ({"x": 1}, None)


def test_run_tool_rejects_bad_arguments_before_touching_the_database(db_path: Path) -> None:
    result = run_tool("search_listings", {"purpose": "lease"}, db_path)
    assert result["error"].startswith("Invalid arguments for search_listings")
    result = run_tool("search_listings", {"purpose": "sale", "city": "Riyadh", "limit": "2"}, db_path)
    assert "error" not in result and result["returned"] <= 2
    missing = run_tool("search_listings", {"purpose": "sale"}, Path("does-not-exist.db"))
    assert "Database unavailable" in missing["error"]          # a valid call reaches the database layer
    bad_and_missing = run_tool("search_listings", {"purpose": "lease"}, Path("does-not-exist.db"))
    assert bad_and_missing["error"].startswith("Invalid arguments")   # an invalid one never does
