"""Turn DarGlobal project pages into structured facts with Claude.

Marketing pages state location, property types, brand partner and prices in prose. One
offline pass extracts them into a table so the agent can filter and compare projects
instead of relying on text search alone. Runs once at ingest time; the deployed app
never calls this.

The model is told to copy only what the page states and to leave fields empty otherwise,
so a missing value in the table means "the page does not say", not "unknown to the model".
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import anthropic
from pydantic import BaseModel, Field

from estatequery.config import get_settings
from estatequery.ingest.build_db import page_type_for, read_jsonl, slug_from_url

MODEL = "claude-opus-5-5"
MAX_PAGE_CHARS = 24000
CONCURRENCY = 4

INSTRUCTIONS = """You are extracting facts from one page of the website of DarGlobal, a real-estate developer.

Fill the fields using only what this page states. Do not use outside knowledge about DarGlobal or its projects, because the result is shown to users as "what DarGlobal's website says". When the page does not state something, use null (or an empty list). Copy prices exactly as written, with their currency.

Set is_project to true only when the page is about one specific development (a tower, a community, a villa collection). Set it to false for category listings, brand-partner profiles, company pages and FAQs."""


class ProjectFacts(BaseModel):
    is_project: bool = Field(description="True only if the page describes one specific development.")
    name: str | None = Field(description="Project name as the page gives it.")
    country: str | None
    city: str | None = Field(description="City or area, e.g. Dubai, Muscat, Jeddah, Marbella.")
    property_types: list[str] = Field(description="e.g. Apartments, Villas, Penthouses, Hotel rooms, Plots.")
    brand_partner: str | None = Field(description="Partner brand, e.g. Trump, Missoni, Pagani, Elie Saab.")
    status: str | None = Field(description="e.g. Launched, Under construction, Completed, with dates if stated.")
    starting_price: str | None = Field(description="Starting price exactly as written, with currency.")
    bedrooms: str | None = Field(description="Bedroom range as stated, e.g. '1-4'.")
    amenities: list[str] = Field(description="Up to 12 notable amenities named on the page.")
    summary: str | None = Field(description="Two factual sentences describing the project.")


async def extract(client: anthropic.AsyncAnthropic, page: dict[str, Any]) -> dict[str, Any] | None:
    text = page.get("text") or ""
    if len(text) > MAX_PAGE_CHARS:
        print(f"  note: {page['url']} is {len(text)} chars; sending the first {MAX_PAGE_CHARS}")
        text = text[:MAX_PAGE_CHARS]
    content = f"URL: {page['url']}\nTitle: {page.get('title')}\n\nPage text:\n{text}"
    try:
        response = await client.messages.parse(
            model=MODEL,
            max_tokens=4000,
            system=INSTRUCTIONS,
            messages=[{"role": "user", "content": content}],
            output_format=ProjectFacts,
            output_config={"effort": "low"},
        )
    except anthropic.RateLimitError:
        await asyncio.sleep(20)
        return await extract(client, page)
    except anthropic.APIStatusError as exc:
        print(f"  failed: {page['url']} ({exc.status_code})")
        return None
    facts = response.parsed_output
    if response.stop_reason != "end_turn" or facts is None:
        print(f"  skipped: {page['url']} (stop_reason={response.stop_reason})")
        return None
    return {"url": page["url"], **facts.model_dump()}


async def run(pages_path: Path, out_path: Path, limit: int | None) -> None:
    settings = get_settings()
    done = {r["url"] for r in read_jsonl(out_path)}
    pages = [
        p for p in read_jsonl(pages_path)
        if page_type_for(slug_from_url(p["url"])) == "project" and p["url"] not in done
    ]
    if limit:
        pages = pages[:limit]
    print(f"{len(pages)} project pages to extract ({len(done)} already done)")
    if not pages:
        return

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=3)
    semaphore = asyncio.Semaphore(CONCURRENCY)
    kept = 0

    async def worker(page: dict[str, Any]) -> None:
        nonlocal kept
        async with semaphore:
            record = await extract(client, page)
        if record is None:
            return
        # Every outcome is recorded so reruns skip finished pages; build_db ignores
        # rows without a name, which is how non-project pages drop out.
        if not record["is_project"]:
            record["name"] = None
        else:
            kept += 1
        with out_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    await asyncio.gather(*(worker(p) for p in pages))
    print(f"done: {kept} projects extracted -> {out_path}")


def main(argv: list[str] | None = None) -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Extract structured facts from DarGlobal project pages.")
    parser.add_argument("--pages", type=Path, default=settings.raw_dir / "darglobal_pages.jsonl")
    parser.add_argument("--out", type=Path, default=settings.raw_dir / "darglobal_projects.jsonl")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)
    asyncio.run(run(args.pages, args.out, args.limit))


if __name__ == "__main__":
    main()
