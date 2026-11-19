"""Collect Wasalt listings by hand, with a person in the loop.

Wasalt's bot protection stops automated visits, and this project does not try to get past it.
Instead a person opens listing pages in their own browser, passes any "verify you are human"
check themselves, and a read-only userscript (tools/wasalt-collector.user.js) saves the listing
data of each page they view. This module supports that route and never touches wasalt.sa:

  estatequery wasalt-urls     build a page of listing links for the person to open
  estatequery import-wasalt   turn the userscript's download into rows in wasalt_listings.jsonl

The import runs every listing through the same allowlist as the automated scraper
(`record_from_next_data`), so owner and agent names, phone numbers and licence numbers never
reach the dataset, even if a saved file still contained them.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from estatequery.config import get_settings
from estatequery.scrape.wasalt import fetch_sitemap, listing_id_from_url, record_from_next_data, stride_sample

LISTING_URL_RE = re.compile(r"^https://wasalt\.sa/en/property/(sale|rent)/[A-Za-z0-9%_\-]+-\d+$")


# --------------------------------------------------------------------------- URL list


def saved_ids(jsonl_path: Path) -> set[str]:
    ids: set[str] = set()
    if jsonl_path.exists():
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                ids.add(str(json.loads(line).get("id")))
    return ids


def pick_urls(urls: list[str], n_sale: int, n_rent: int, seen_ids: set[str]) -> list[str]:
    """Evenly spaced listing URLs, sale first then rent, skipping listings already saved.

    The spacing is offset by half a step so the picks fall between the ones the automated
    sample already used, and a few extra are drawn so filtering out saved ones still leaves n.
    """

    def choose(pool: list[str], n: int) -> list[str]:
        if n <= 0 or not pool:
            return []
        offset = len(pool) // (2 * n)
        rotated = pool[offset:] + pool[:offset]
        chosen: list[str] = []
        taken: set[str] = set()
        for url in stride_sample(rotated, n + len(seen_ids)):
            listing_id = listing_id_from_url(url)
            if listing_id not in seen_ids and listing_id not in taken:
                taken.add(listing_id)
                chosen.append(url)
        return chosen[:n]

    sale = [u for u in urls if "/property/sale/" in u]
    rent = [u for u in urls if "/property/rent/" in u]
    return choose(sale, n_sale) + choose(rent, n_rent)


def label_for(url: str) -> str:
    kind = "For sale" if "/property/sale/" in url else "For rent"
    slug = re.sub(r"-\d+$", "", url.rstrip("/").rsplit("/", 1)[-1])
    return f"{kind}: {slug.replace('-', ' ')}"


def render_open_page(urls: list[str]) -> str:
    items = "\n".join(
        f'<li><a href="{html.escape(u, quote=True)}" target="_blank" rel="noopener noreferrer">'
        f"{html.escape(label_for(u))}</a></li>"
        for u in urls
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wasalt listings to open ({len(urls)})</title>
<style>
  body {{ font: 16px/1.5 system-ui, sans-serif; max-width: 760px; margin: 2rem auto; padding: 0 1rem; color: #1d2420; background: #fbfaf7; }}
  h1 {{ font-size: 1.4rem; }} li {{ margin: .35rem 0; }}
  a {{ color: #1f6f5c; }} a:visited {{ color: #8a8f8c; text-decoration: line-through; }}
  .note {{ background: #eef4f1; border-radius: 10px; padding: .8rem 1rem; }}
</style></head><body>
<h1>Wasalt listings to open ({len(urls)})</h1>
<p class="note">Open each link in your own Chrome, at your own pace. If a "verify you are human" check
appears, pass it yourself. With the EstateQuery collector installed, each listing is saved as it loads
(the badge in the corner counts them). Opened links turn grey. When you are done, click Download in the badge.</p>
<ol>
{items}
</ol>
</body></html>
"""


def urls_main(argv: list[str] | None = None) -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Build a page of Wasalt listing links to open by hand.")
    parser.add_argument("--sale", type=int, default=30)
    parser.add_argument("--rent", type=int, default=30)
    parser.add_argument("--jsonl", type=Path, default=settings.raw_dir / "wasalt_listings.jsonl")
    parser.add_argument("--out", type=Path, default=settings.raw_dir / "wasalt_to_open.html")
    args = parser.parse_args(argv)

    sitemap = asyncio.run(fetch_sitemap())  # the public sitemap file, as the scraper reads it
    urls = pick_urls(sitemap, args.sale, args.rent, saved_ids(args.jsonl))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_open_page(urls), encoding="utf-8")
    args.out.with_suffix(".txt").write_text("\n".join(urls) + "\n", encoding="utf-8")
    sale = sum("/property/sale/" in u for u in urls)
    print(f"{len(urls)} links ({sale} sale, {len(urls) - sale} rent) -> {args.out}")


# --------------------------------------------------------------------------- import


@dataclass
class ImportResult:
    records: list[dict[str, Any]] = field(default_factory=list)
    duplicates: int = 0
    rejected: list[str] = field(default_factory=list)


def import_entries(entries: Any, existing_ids: set[str]) -> ImportResult:
    """Turn collector entries ({id, url, details}) into allowlisted listing records.

    Never raises on bad input: an entry that cannot be used is counted and described.
    """
    result = ImportResult()
    if isinstance(entries, dict):
        entries = list(entries.values())
    if not isinstance(entries, list):
        result.rejected.append("file is not a list of entries")
        return result

    seen = set(existing_ids)
    for index, entry in enumerate(entries):
        where = f"entry {index + 1}"
        if not isinstance(entry, dict):
            result.rejected.append(f"{where}: not an object")
            continue
        url, details = entry.get("url"), entry.get("details")
        if not isinstance(url, str) or not LISTING_URL_RE.match(url):
            result.rejected.append(f"{where}: not a Wasalt listing URL")
            continue
        if not isinstance(details, dict) or details.get("id") is None:
            result.rejected.append(f"{where}: no listing data")
            continue
        listing_id = str(details["id"])
        if listing_id in seen:
            result.duplicates += 1
            continue
        try:
            record = record_from_next_data({"props": {"pageProps": {"propertyDetailsV3": details}}}, url)
        except Exception as exc:  # noqa: BLE001 - one odd entry must not stop the rest
            result.rejected.append(f"{where}: could not be read ({type(exc).__name__})")
            continue
        if record is None or record.get("id") is None:
            result.rejected.append(f"{where}: listing is not an approved, public listing")
            continue
        seen.add(listing_id)
        result.records.append(record)
    return result


def import_main(argv: list[str] | None = None) -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Import listings saved by the Wasalt collector userscript.")
    parser.add_argument("file", type=Path, help="the wasalt-collected.json downloaded from the collector's badge")
    parser.add_argument("--out", type=Path, default=settings.raw_dir / "wasalt_listings.jsonl")
    args = parser.parse_args(argv)

    try:
        entries = json.loads(args.file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"could not read {args.file}: {exc}") from exc

    result = import_entries(entries, saved_ids(args.out))
    if result.records:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("a", encoding="utf-8") as fh:
            for record in result.records:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"imported {len(result.records)} | already saved {result.duplicates} | rejected {len(result.rejected)}"
          f" -> {args.out}")
    for reason in result.rejected[:10]:
        print(f"  rejected: {reason}")
    print(f"total listings now: {len(saved_ids(args.out))}")
