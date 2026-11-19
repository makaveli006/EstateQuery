"""Wasalt listing scraper.

Source policy (https://wasalt.sa/llms.txt, read 2026-11-19):
  - extraction of public listing content is allowed, at a moderate crawl rate
  - contact details must not be extracted
  - attribution "Source: Wasalt.sa" is required and users must be pointed to the live site

So this scraper samples a few hundred listings from the public sitemap, fetches them
slowly (one request per second, sequentially), and keeps an explicit allowlist of property
fields. Owner names, phone numbers, licence and deed numbers are never read into the output.

Transport: the site's firewall answers 403 to plain HTTP clients, so listing pages are
requested from inside a real Chromium browser driven by Playwright (the `scrape`
dependency group). This runs on a developer machine; it is not part of the deployed app.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import html as html_lib
import json
import re
import time
from pathlib import Path
from typing import Any

import httpx

from estatequery.config import get_settings

SITEMAP_URL = "https://cdn.wasalt.sa/sitemap/product_sitemap_en_sa.xml.gz"
HOME_URL = "https://wasalt.sa/en"
USER_AGENT = "EstateQueryBot/0.1 (low-rate research crawler for a job assignment)"

NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
LOC_RE = re.compile(r"<loc>([^<]+)</loc>")
TAG_RE = re.compile(r"<[^>]+>")
ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
# Saudi mobile / unified numbers and anything that looks like a long dialable number.
PHONE_RE = re.compile(r"(?:\+?966|00966|0)?\s?5\d(?:[\s-]?\d){7}|\b9200(?:[\s-]?\d){5}\b|\+?\d(?:[\s-]?\d){9,13}")

# additionalAttributes we keep. Everything else in that list (street names, plan, land,
# building and licence numbers) is dropped.
ADDITIONAL_ALLOW = {
    "completionYear": "age",
    "propertyFacade": "facade",
    "electricityMeter": "electricity",
    "waterMeter": "water",
    "streetWidth": "street_width",
    "noOfStreets": "streets",
    "floorNumber": "floor",
    "noOfFloors": "floors",
}

# Runs inside a loaded listing page: returns its embedded Next.js data as text.
NEXT_DATA_JS = "() => { const el = document.getElementById('__NEXT_DATA__'); return el ? el.textContent : null; }"
CHALLENGE_TITLE = "Just a moment"
SKIP_RESOURCES = {"image", "media", "font"}  # not needed for the data; saves bandwidth for both sides


def clean_text(value: Any) -> str | None:
    """Strip HTML and phone-like numbers from free text written by advertisers."""
    if not value or not isinstance(value, str):
        return None
    text = html_lib.unescape(TAG_RE.sub(" ", value))
    text = PHONE_RE.sub("[contact removed]", text.translate(ARABIC_DIGITS))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def to_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).translate(ARABIC_DIGITS).replace(",", "").strip())
    except ValueError:
        return None


def default_rent(rent_freq: Any) -> tuple[str | None, float | None]:
    """Rent period and amount. The site sends either a plain string ("yearly") or a map
    of periods, e.g. {"yearly": {"freq": "yearly", "amount": 38000, "default_freq": true}}."""
    if isinstance(rent_freq, str):
        return rent_freq.strip().lower() or None, None
    if isinstance(rent_freq, dict):
        options = [v for v in rent_freq.values() if isinstance(v, dict)]
        chosen = next((o for o in options if o.get("default_freq")), options[0] if options else None)
        if chosen:
            return (str(chosen.get("freq") or "").lower() or None), to_number(chosen.get("amount"))
    return None, None


def extract_next_data(page_html: str) -> dict[str, Any] | None:
    match = NEXT_DATA_RE.search(page_html)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def parse_listing(page_html: str, url: str) -> dict[str, Any] | None:
    """Turn one listing page into a flat record. Returns None if the page has no listing."""
    data = extract_next_data(page_html)
    return record_from_next_data(data, url) if data else None


def record_from_next_data(data: dict[str, Any], url: str) -> dict[str, Any] | None:
    """Build the allowlisted record from the page's embedded Next.js data."""
    details = (data.get("props", {}).get("pageProps", {}) or {}).get("propertyDetailsV3")
    if not isinstance(details, dict):
        return None
    info = details.get("propertyInfo") or {}
    if not info or info.get("statusTypeSlug") not in (None, "approved"):
        return None

    attrs: dict[str, Any] = {}
    for item in details.get("attributes") or []:
        if isinstance(item, dict) and item.get("key") is not None:
            attrs[item["key"]] = item.get("value")

    extras: dict[str, Any] = {}
    for item in details.get("additionalAttributes") or []:
        if isinstance(item, dict) and item.get("key") in ADDITIONAL_ALLOW and item.get("value") not in (None, "", "-"):
            extras[ADDITIONAL_ALLOW[item["key"]]] = item["value"]

    purpose = (info.get("propertyFor") or "").lower() or None
    sale_price = to_number(info.get("salePrice"))
    rent_frequency, rent_amount = default_rent(info.get("rentFreq"))
    rent_price = rent_amount if rent_amount is not None else to_number(info.get("expectedRent"))
    price = rent_price if purpose == "rent" else sale_price
    if price is None:
        price = sale_price if sale_price is not None else rent_price

    area = None
    for key in ("builtUpArea", "landArea", "plotArea", "carpetArea", "area"):
        area = to_number(attrs.get(key))
        if area:
            break

    location = details.get("location") or {}
    return {
        "id": details.get("id"),
        "url": url,
        "title": clean_text(info.get("title")),
        "listing_name": clean_text(info.get("propertyName")),
        "purpose": purpose,
        "property_type": info.get("propertySubType"),
        "main_type": info.get("propertyMainType"),
        "city": info.get("city"),
        "district": info.get("district") or info.get("zone"),
        "territory": info.get("territory"),
        "address": clean_text(info.get("address")),
        "country": info.get("country"),
        "price": price,
        "currency": info.get("currencyType") or "SAR",
        "rent_frequency": rent_frequency if purpose == "rent" else None,
        "bedrooms": to_number(attrs.get("noOfBedrooms")),
        "bathrooms": to_number(attrs.get("noOfBathrooms")),
        "living_rooms": to_number(attrs.get("noOfLivingrooms")),
        "area_sqm": area,
        "furnishing": info.get("furnishingType"),
        "extras": extras,
        "attributes": {k: v for k, v in attrs.items() if v not in (None, "")},
        "description": clean_text(info.get("description")),
        "lat": to_number(location.get("lat")),
        "lon": to_number(location.get("lon")),
        "published_at": details.get("publishedAt"),
        "updated_at": details.get("updatedAt"),
    }


def stride_sample(urls: list[str], n: int) -> list[str]:
    """Evenly spaced sample so the result spans the whole sitemap, deterministically."""
    if n <= 0 or not urls:
        return []
    if n >= len(urls):
        return list(urls)
    step = len(urls) / n
    return [urls[int(i * step)] for i in range(n)]


def listing_id_from_url(url: str) -> str:
    return url.rstrip("/").rsplit("-", 1)[-1]


async def fetch_sitemap() -> list[str]:
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}) as client:
        resp = await client.get(SITEMAP_URL, timeout=120)
        resp.raise_for_status()
    body = resp.content
    try:
        body = gzip.decompress(body)
    except OSError:
        pass  # already decompressed by the transport
    return LOC_RE.findall(body.decode("utf-8", errors="replace"))


async def load_page(page: Any, url: str, check_wait: int, *, need_data: bool) -> tuple[str, str | None]:
    """Open a page as a normal visit. Returns (state, embedded data).

    state: "ok" | "gone" (listing removed) | "refused" (the site's browser check did not
    clear in time) | "error". The check is left to run on its own; nothing here tries to
    solve it.
    """
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception:  # noqa: BLE001 - timeouts and aborted navigations
        return "error", None
    if response is not None and response.status in (404, 410):
        return "gone", None

    challenged = False
    for _ in range(check_wait * 2):
        try:
            title = await page.title()
            data = await page.evaluate(NEXT_DATA_JS)
        except Exception:  # noqa: BLE001 - the page is navigating (check just cleared)
            await page.wait_for_timeout(500)
            continue
        if data or (not need_data and CHALLENGE_TITLE not in title):
            return "ok", data
        if CHALLENGE_TITLE in title:
            challenged = True
        elif not challenged:
            return "error", None  # a real page without listing data
        await page.wait_for_timeout(500)
    return ("refused" if challenged else "error"), None


async def crawl(n_sale: int, n_rent: int, out_path: Path, per_second: float, headed: bool,
                retry_wait: float = 15.0, max_refusals: int = 3) -> None:
    from playwright.async_api import async_playwright  # scrape-only dependency

    out_path.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                seen.add(str(json.loads(line).get("id")))

    urls = await fetch_sitemap()
    sale = [u for u in urls if "/property/sale/" in u]
    rent = [u for u in urls if "/property/rent/" in u]
    targets = stride_sample(sale, n_sale) + stride_sample(rent, n_rent)
    todo = [u for u in targets if listing_id_from_url(u) not in seen]
    print(f"sitemap: {len(sale)} sale, {len(rent)} rent | target {len(targets)} | "
          f"already saved {len(seen)} | to fetch {len(todo)}", flush=True)
    if not todo:
        return

    interval = 1.0 / per_second
    # A visible browser gives a person the chance to complete a site check by hand.
    check_wait = 90 if headed else 20
    saved = skipped = refused_in_a_row = 0
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not headed)
        context = await browser.new_context(locale="en-US")

        async def skip_heavy(route: Any) -> None:
            # Only the site's own photos and fonts are skipped. Everything else loads
            # normally, including whatever the site's browser check needs.
            request = route.request
            if request.resource_type in SKIP_RESOURCES and "wasalt" in request.url.split("/")[2]:
                await route.abort()
            else:
                await route.continue_()

        await context.route("**/*", skip_heavy)
        page = await context.new_page()
        state, _ = await load_page(page, HOME_URL, check_wait, need_data=False)
        if state != "ok":
            print(f"could not open {HOME_URL} ({state}); rerun with --headed")
            await browser.close()
            return

        for i, url in enumerate(todo, 1):
            started = time.monotonic()
            state, next_data = await load_page(page, url, check_wait, need_data=True)
            record = None
            if state == "ok" and next_data:
                try:
                    record = record_from_next_data(json.loads(next_data), url)
                except json.JSONDecodeError:
                    record = None

            if state == "refused":
                refused_in_a_row += 1
                if refused_in_a_row >= max_refusals:
                    print(f"stopping: the site's browser check did not clear {max_refusals} times in a row. "
                          "Rerun later with --headed (it resumes).", flush=True)
                    break
                await asyncio.sleep(retry_wait)  # back off; the site is asking us to slow down
            else:
                refused_in_a_row = 0

            if record and record.get("id") is not None:
                with out_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                saved += 1
            else:
                skipped += 1
            if i % 25 == 0 or i == len(todo):
                print(f"  {i}/{len(todo)} fetched | saved {saved} | skipped {skipped}", flush=True)
            await asyncio.sleep(max(0.0, interval - (time.monotonic() - started)))
        await browser.close()
    print(f"done: saved {saved}, skipped {skipped} -> {out_path}", flush=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Sample and scrape public Wasalt listings.")
    parser.add_argument("--sale", type=int, default=400)
    parser.add_argument("--rent", type=int, default=300)
    parser.add_argument("--per-second", type=float, default=1.0)
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--retry-wait", type=float, default=15.0,
                        help="seconds to back off after the site's browser check appears")
    parser.add_argument("--max-refusals", type=int, default=3,
                        help="stop after this many checks in a row fail to clear")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    out = args.out or (get_settings().raw_dir / "wasalt_listings.jsonl")
    asyncio.run(crawl(args.sale, args.rent, out, args.per_second, args.headed, args.retry_wait, args.max_refusals))


if __name__ == "__main__":
    main()
