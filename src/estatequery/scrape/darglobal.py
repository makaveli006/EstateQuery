"""DarGlobal website scraper.

Collects the public project, partner and company pages listed in the site's own sitemap
(robots.txt allows them). The site sits behind a browser check, so pages are loaded in a
real Chromium browser driven by Playwright, one at a time with a pause between pages.
No stealth plugins and no CAPTCHA solving: if the site asks for a human, rerun with
`--headed` and complete the check by hand.

Output: one JSON line per page with the title, meta description, headings and visible
text (site header, navigation and footer excluded).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from estatequery.config import get_settings

BASE_URL = "https://darglobal.co.uk"
LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")

# Sections that are news/marketing funnels or utility pages rather than project content.
EXCLUDE_PREFIXES = ("blog", "press", "insights", "landingpage", "landing-page", "pay-online", "campaigns")
EXCLUDE_SLUGS = {
    "thank-you", "success", "bookyourunit", "careers", "internship-application", "internships",
    "privacy-policy", "terms-conditions", "terms-of-uses", "get-in-touch", "become-a-broker",
    "become-an-agent", "win-a-trip-to-dubai", "broker-contest-terms-conditions", "new-home-page",
    "aston-martin-media", "sohar-islamic", "darglobal-insignia-card", "invest-crypto",
    "darglobal-exclusive-off-plan-investment-catalogue",
}

SITEMAP_JS = "async () => { const r = await fetch('/sitemap.xml'); return r.ok ? await r.text() : ''; }"

SCROLL_JS = """
async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  for (let y = 0, n = 0; y < document.body.scrollHeight && n < 40; y += 900, n++) {
    window.scrollTo(0, y);
    await sleep(120);
  }
  window.scrollTo(0, 0);
}
"""

EXTRACT_JS = """
() => {
  const chrome = Array.from(document.querySelectorAll('header, nav, footer'));
  const previous = chrome.map((e) => e.style.display);
  chrome.forEach((e) => { e.style.display = 'none'; });   // innerText skips hidden elements
  const text = document.body ? document.body.innerText : '';
  const headings = Array.from(document.querySelectorAll('h1, h2, h3'))
    .map((e) => e.innerText.trim()).filter((t) => t && t.length <= 120);
  chrome.forEach((e, i) => { e.style.display = previous[i]; });
  const meta = document.querySelector('meta[name="description"]');
  return {
    title: document.title,
    description: meta ? meta.content : null,
    headings: Array.from(new Set(headings)).slice(0, 80),
    text,
  };
}
"""


def slug_of(url: str) -> str:
    return urlparse(url).path.strip("/").lower()


def select_urls(sitemap_xml: str, with_press: bool = False) -> list[str]:
    """Pick content pages from the sitemap, de-duplicated, in a stable order."""
    seen: set[str] = set()
    chosen: list[str] = []
    for url in LOC_RE.findall(sitemap_xml):
        if urlparse(url).netloc.lower().removeprefix("www.") != "darglobal.co.uk":
            continue
        slug = slug_of(url)
        first = slug.split("/", 1)[0]
        if slug in seen or slug in EXCLUDE_SLUGS:
            continue
        if first in EXCLUDE_PREFIXES and not (with_press and first == "press" and "/" in slug):
            continue
        seen.add(slug)
        chosen.append(url)
    return chosen


def tidy_text(text: str) -> str:
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in text.splitlines()]
    out: list[str] = []
    for line in lines:
        if line and (not out or out[-1] != line):  # drop blanks and immediate repeats (carousels)
            out.append(line)
    return "\n".join(out)


async def wait_for_real_page(page: Any, seconds: int) -> bool:
    """The browser check serves a tiny stub first and then reloads into the real page."""
    for _ in range(seconds * 2):
        html = await page.content()
        if len(html) > 5000 and "Incapsula incident" not in html:
            return True
        await page.wait_for_timeout(500)
    return False


async def crawl(out_path: Path, limit: int | None, delay: float, headed: bool, with_press: bool) -> None:
    from playwright.async_api import async_playwright  # scrape-only dependency

    out_path.parent.mkdir(parents=True, exist_ok=True)
    done: set[str] = set()
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                done.add(slug_of(json.loads(line)["url"]))

    saved = skipped = blocked = 0
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not headed)
        page = await (await browser.new_context(locale="en-GB", viewport={"width": 1366, "height": 900})).new_page()

        await page.goto(BASE_URL + "/", wait_until="domcontentloaded", timeout=60000)
        if not await wait_for_real_page(page, 60 if headed else 25):
            print("the site did not get past its browser check. Rerun with --headed and complete it by hand.")
            await browser.close()
            return

        urls = select_urls(await page.evaluate(SITEMAP_JS), with_press)
        todo = [u for u in urls if slug_of(u) not in done]
        if limit:
            todo = todo[:limit]
        print(f"sitemap: {len(urls)} content pages | already saved {len(done)} | to fetch {len(todo)}", flush=True)

        for i, url in enumerate(todo, 1):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                if not await wait_for_real_page(page, 20):
                    blocked += 1
                    print(f"  blocked: {url}", flush=True)
                    if blocked >= 3:
                        print("stopping: blocked 3 times. Rerun with --headed (it resumes).")
                        break
                    continue
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:  # noqa: BLE001 - pages with long-polling never go idle
                    pass
                await page.evaluate(SCROLL_JS)
                data = await page.evaluate(EXTRACT_JS)
            except Exception as exc:  # noqa: BLE001
                skipped += 1
                print(f"  failed: {url} ({type(exc).__name__})", flush=True)
                continue

            text = tidy_text(data.get("text") or "")
            if len(text) < 200:
                skipped += 1
                print(f"  thin page skipped: {url} ({len(text)} chars)", flush=True)
            else:
                record = {
                    "url": url,
                    "title": (data.get("title") or "").strip() or None,
                    "description": data.get("description"),
                    "headings": data.get("headings") or [],
                    "text": text,
                    "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
                with out_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                saved += 1
            if i % 10 == 0 or i == len(todo):
                print(f"  {i}/{len(todo)} | saved {saved} | skipped {skipped} | blocked {blocked}", flush=True)
            await asyncio.sleep(delay)
        await browser.close()
    print(f"done: saved {saved}, skipped {skipped}, blocked {blocked} -> {out_path}", flush=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Scrape DarGlobal project, partner and company pages.")
    parser.add_argument("--limit", type=int, default=None, help="only fetch the first N pages (for testing)")
    parser.add_argument("--delay", type=float, default=2.5, help="seconds to wait between pages")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--with-press", action="store_true", help="also fetch press releases")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    out = args.out or (get_settings().raw_dir / "darglobal_pages.jsonl")
    asyncio.run(crawl(out, args.limit, args.delay, args.headed, args.with_press))


if __name__ == "__main__":
    main()
