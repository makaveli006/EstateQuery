"""Argument validation for the agent's tools.

The model proposes a tool call and the application decides whether it may run. Every call is
checked here against a Pydantic model before any handler or SQL is touched. A bad call comes
back as a structured error that names each invalid argument and lists the valid ones, so the
model can correct itself in the same turn.

The checks are deliberately forgiving where the model is usually right in spirit (numbers sent
as text such as "1,500,000", a limit above the maximum is clamped, "Sale" for "sale") and strict
where a wrong value would silently return nothing or the wrong thing (unknown argument names,
values outside an enum, a minimum above its maximum).

These models must list exactly the properties of the matching entry in `tools.TOOL_SPECS`; a
test fails if the two drift apart.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

SORT_VALUES = ("price_asc", "price_desc", "newest", "area_desc", "price_per_sqm_asc")
GROUP_VALUES = ("city", "district", "property_type", "bedrooms")
MAX_TEXT = 200


def _number(value: Any) -> Any:
    """Accept "1,500,000" and " 80000 " as numbers; leave anything else for Pydantic to judge."""
    if isinstance(value, str):
        cleaned = re.sub(r"[,\s_]", "", value)
        if cleaned and re.fullmatch(r"-?\d+(\.\d+)?", cleaned):
            return float(cleaned)
    return value


def _clamp(low: int, high: int, default: int):
    """A validator that pulls a count into range instead of rejecting it (the handlers always clamped)."""

    def check(value: Any) -> int:
        value = _number(value)
        if value is None or value == "":
            return default
        try:
            return max(low, min(int(float(value)), high))
        except (TypeError, ValueError):
            raise ValueError("must be a whole number") from None

    return check


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ListingFilters(Args):
    purpose: Literal["sale", "rent"] | None = None
    city: str | None = Field(None, max_length=MAX_TEXT)
    district: str | None = Field(None, max_length=MAX_TEXT)
    property_type: str | None = Field(None, max_length=MAX_TEXT)
    min_price: float | None = Field(None, ge=0)
    max_price: float | None = Field(None, ge=0)
    min_bedrooms: float | None = Field(None, ge=0, le=100)
    max_bedrooms: float | None = Field(None, ge=0, le=100)
    min_area_sqm: float | None = Field(None, ge=0)
    max_area_sqm: float | None = Field(None, ge=0)

    @field_validator("purpose", mode="before")
    @classmethod
    def _purpose(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("min_price", "max_price", "min_bedrooms", "max_bedrooms", "min_area_sqm", "max_area_sqm",
                     mode="before")
    @classmethod
    def _numbers(cls, value: Any) -> Any:
        return _number(value)

    @model_validator(mode="after")
    def _ranges(self) -> "ListingFilters":
        for low, high in (("min_price", "max_price"), ("min_bedrooms", "max_bedrooms"),
                          ("min_area_sqm", "max_area_sqm")):
            a, b = getattr(self, low), getattr(self, high)
            if a is not None and b is not None and a > b:
                raise ValueError(f"{low} ({a:g}) is above {high} ({b:g})")
        return self


class SearchListingsArgs(ListingFilters):
    keywords: str | None = Field(None, max_length=MAX_TEXT)
    sort: Literal["price_asc", "price_desc", "newest", "area_desc", "price_per_sqm_asc"] | None = None
    limit: int = 5

    @field_validator("limit", mode="before")
    @classmethod
    def _limit(cls, value: Any) -> int:
        return _clamp(1, 8, 5)(value)


class ListingStatsArgs(ListingFilters):
    group_by: Literal["city", "district", "property_type", "bedrooms"]


class GetListingArgs(Args):
    id: int = Field(..., ge=1)

    @field_validator("id", mode="before")
    @classmethod
    def _id(cls, value: Any) -> Any:
        value = _number(value)
        return int(value) if isinstance(value, float) and value.is_integer() else value


class ListProjectsArgs(Args):
    country: str | None = Field(None, max_length=MAX_TEXT)
    property_type: str | None = Field(None, max_length=MAX_TEXT)
    brand: str | None = Field(None, max_length=MAX_TEXT)


class SearchDarglobalArgs(Args):
    query: str = Field(..., min_length=1, max_length=500)
    limit: int = 4

    @field_validator("limit", mode="before")
    @classmethod
    def _limit(cls, value: Any) -> int:
        return _clamp(1, 6, 4)(value)


class GetProjectArgs(Args):
    project: str = Field(..., min_length=1, max_length=MAX_TEXT)


MODELS: dict[str, type[Args]] = {
    "search_listings": SearchListingsArgs,
    "listing_stats": ListingStatsArgs,
    "get_listing": GetListingArgs,
    "list_darglobal_projects": ListProjectsArgs,
    "search_darglobal": SearchDarglobalArgs,
    "get_darglobal_project": GetProjectArgs,
}


def _problem(error: dict[str, Any]) -> dict[str, str]:
    where = ".".join(str(part) for part in error.get("loc", ())) or "arguments"
    kind = error.get("type", "")
    if kind == "extra_forbidden":
        message = "is not an argument of this tool"
    elif kind == "missing":
        message = "is required"
    elif kind == "literal_error":
        message = f"must be one of {error.get('ctx', {}).get('expected', '')}".strip()
    else:
        message = str(error.get("msg", "is invalid")).removeprefix("Value error, ")
    return {"arg": where, "problem": message}


def validate_args(name: str, args: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return (clean_args, None) when the call is valid, or (None, error_result) when it is not.

    An unknown tool name is not judged here; `run_tool` reports that itself.
    """
    model = MODELS.get(name)
    if model is None:
        return args, None
    try:
        parsed = model.model_validate(args)
    except ValidationError as exc:
        problems = [_problem(e) for e in exc.errors(include_url=False)]
        summary = "; ".join(f"{p['arg']} {p['problem']}" for p in problems)
        return None, {
            "error": f"Invalid arguments for {name}: {summary}",
            "invalid": problems,
            "valid_arguments": sorted(model.model_fields),
        }
    return parsed.model_dump(exclude_none=True), None
