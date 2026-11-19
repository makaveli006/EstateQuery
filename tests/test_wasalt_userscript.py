"""The collector userscript, run in a real (headless) browser against fixture pages.

Playwright answers every wasalt.sa request from a fixture and aborts everything else, so these
tests never contact the real site. Skipped when Playwright or Chromium is not installed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from estatequery.scrape.wasalt_manual import import_entries

sync_api = pytest.importorskip("playwright.sync_api")

SCRIPT = (Path(__file__).resolve().parent.parent / "tools" / "wasalt-collector.user.js").read_text(encoding="utf-8")
# Tampermonkey provides GM_*; here they are backed by localStorage, which persists across pages.
SHIM = (
    "window.GM_getValue=(k,d)=>{const v=localStorage.getItem('gm:'+k);return v===null?d:v};"
    "window.GM_setValue=(k,v)=>localStorage.setItem('gm:'+k,v);window.__EQ_WAIT_MS=1500;"
)
PRIVATE = ("PRIVATE OWNER NAME", "PRIVATE EMPLOYEE", "9999000011", "1111222233334444", "PRIVATE-IMAGE-URL")


def next_data(listing_id: int) -> dict:
    return {"props": {"pageProps": {
        "reqHeaders": {"cookie": "PRIVATE-COOKIE"},                      # not in the kept fields
        "propertyDetailsV3": {
            "id": listing_id,
            "location": {"lat": "24.7", "lon": "46.6"},
            "attributes": [{"key": "noOfBedrooms", "value": 3}, {"key": "builtUpArea", "value": "120"}],
            "additionalAttributes": [{"key": "completionYear", "value": "New"}],
            "publishedAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-02T00:00:00Z",
            "leadContactInfo": {"ownerName": "PRIVATE OWNER NAME"},
            "propertyOwner": {"resEmployee": "PRIVATE EMPLOYEE", "falLicenseNumber": "9999000011"},
            "regaVerifiedInfo": [{"fields": [{"key": "deedNumber", "value": "1111222233334444"}]}],
            "galleryImages": ["PRIVATE-IMAGE-URL"],                      # a big field the collector drops
            "propertyInfo": {"title": f"Apartment {listing_id}", "propertyFor": "sale", "propertySubType": "Apartment",
                             "propertyMainType": "Residential", "city": "Riyadh", "district": "Al-Malqa",
                             "salePrice": 900000 + listing_id, "currencyType": "SAR", "statusTypeSlug": "approved",
                             "description": "<p>A flat.</p>"},
        }}}}


def listing_page(listing_id: int) -> str:
    blob = json.dumps(next_data(listing_id))
    return f'<html><head><title>Listing</title></head><body><h1>Listing {listing_id}</h1><script id="__NEXT_DATA__" type="application/json">{blob}</script></body></html>'


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            instance = p.chromium.launch()
        except Exception:  # noqa: BLE001 - Chromium not installed on this machine
            pytest.skip("Chromium is not installed")
        yield instance
        instance.close()


@pytest.fixture()
def site(browser):
    """A browser context that serves fixture listing pages for wasalt.sa and blocks all else."""
    ctx = browser.new_context(accept_downloads=True)
    outside: list[str] = []

    def handle(route):
        url = route.request.url
        if url.startswith("https://wasalt.sa/en/property/"):
            listing = url.rstrip("/").rsplit("-", 1)[-1]
            body = "<html><body>no listing here</body></html>" if listing == "9" else listing_page(int(listing))
            route.fulfill(status=200, content_type="text/html", body=body)
        elif url.startswith(("blob:", "data:")):
            route.continue_()
        else:
            outside.append(url)
            route.abort()

    ctx.route("**/*", handle)
    page = ctx.new_page()
    page.outside = outside  # type: ignore[attr-defined]
    yield page
    ctx.close()


def visit(page, listing_id: int, expect_badge: bool = True) -> None:
    page.goto(f"https://wasalt.sa/en/property/sale/apartment-{listing_id}", wait_until="load")
    page.add_script_tag(content=SHIM + SCRIPT)
    if expect_badge:
        page.wait_for_selector("#eq-collector", timeout=5000)
    else:
        page.wait_for_timeout(2200)  # longer than the collector's shortened wait


def count(page) -> str:
    return page.locator("#eq-collector-count").inner_text()


def test_saves_each_listing_once_and_counts_them(site) -> None:
    visit(site, 5000001)
    assert count(site) == "EstateQuery: 1 saved"
    visit(site, 5000001)                                  # the same page opened again
    assert count(site) == "EstateQuery: 1 saved"
    visit(site, 5000002)
    assert count(site) == "EstateQuery: 2 saved"
    assert site.url.endswith("apartment-5000002")         # it never navigated anywhere
    assert site.outside == []                             # and never reached beyond the page itself


def test_does_nothing_on_a_page_without_listing_data(site) -> None:
    visit(site, 9, expect_badge=False)
    assert site.locator("#eq-collector").count() == 0


def test_download_holds_only_allowed_fields_and_imports_cleanly(site, tmp_path: Path) -> None:
    visit(site, 5000001)
    visit(site, 5000002)
    with site.expect_download() as download:
        site.locator("#eq-collector button", has_text="Download").click()
    saved = tmp_path / "wasalt-collected.json"
    download.value.save_as(saved)
    raw = saved.read_text(encoding="utf-8")

    entries = json.loads(raw)
    assert sorted(e["id"] for e in entries) == ["5000001", "5000002"]
    assert all(e["url"].startswith("https://wasalt.sa/en/property/") for e in entries)
    assert set(entries[0]["details"]) <= {"id", "location", "attributes", "additionalAttributes",
                                          "propertyInfo", "publishedAt", "updatedAt"}
    for secret in (*PRIVATE, "PRIVATE-COOKIE"):
        assert secret not in raw                          # never saved in the first place

    result = import_entries(entries, set())               # and the importer accepts the file as is
    assert [r["id"] for r in result.records] == [5000001, 5000002] and not result.rejected
    assert sorted(r["price"] for r in result.records) == [5900001, 5900002]
    assert {r["city"] for r in result.records} == {"Riyadh"}
