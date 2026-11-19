from __future__ import annotations

import json
from pathlib import Path

from estatequery.scrape.wasalt_manual import (
    LISTING_URL_RE,
    import_entries,
    import_main,
    pick_urls,
    render_open_page,
    saved_ids,
)

PRIVATE = ("PRIVATE OWNER NAME", "PRIVATE EMPLOYEE", "9999000011", "1111222233334444", "0551234567")


def details(listing_id: int = 5000001, status: str = "approved", **info) -> dict:
    return {
        "id": listing_id,
        "location": {"lat": "24.7", "lon": "46.6"},
        "attributes": [{"key": "noOfBedrooms", "value": 3}, {"key": "builtUpArea", "value": "133"}],
        "additionalAttributes": [{"key": "completionYear", "value": "New"}, {"key": "landNumber", "value": "3991"}],
        "publishedAt": "2026-01-28T07:49:47.523Z",
        "updatedAt": "2026-01-28T13:45:27.565Z",
        # personal data that a saved file could still carry; the importer must never keep it
        "leadContactInfo": {"ownerName": "PRIVATE OWNER NAME"},
        "propertyOwner": {"resEmployee": "PRIVATE EMPLOYEE", "falLicenseNumber": "9999000011"},
        "regaVerifiedInfo": [{"fields": [{"key": "deedNumber", "value": "1111222233334444"}]}],
        "propertyInfo": {
            "title": "Apartment with 3 Bedrooms", "propertyFor": "sale", "propertySubType": "Apartment",
            "propertyMainType": "Residential", "city": "Riyadh", "district": "Al-Mahdiyah",
            "address": "Al-Mahdiyah, Riyadh", "salePrice": 850000, "currencyType": "SAR",
            "statusTypeSlug": status, "description": "<p>Nice flat. Call 0551234567</p>", **info,
        },
    }


def entry(listing_id: int = 5000001, **kw) -> dict:
    return {"id": str(listing_id), "url": f"https://wasalt.sa/en/property/sale/apartment-{listing_id}",
            "collected_at": "2026-11-19T00:00:00Z", "details": details(listing_id, **kw)}


def test_a_valid_entry_is_imported_with_the_allowlisted_fields() -> None:
    result = import_entries([entry()], set())
    assert len(result.records) == 1 and result.duplicates == 0 and not result.rejected
    record = result.records[0]
    assert record["id"] == 5000001 and record["price"] == 850000 and record["bedrooms"] == 3
    assert record["city"] == "Riyadh" and record["area_sqm"] == 133
    assert record["url"] == "https://wasalt.sa/en/property/sale/apartment-5000001"


def test_personal_data_never_reaches_the_records_even_if_the_file_still_has_it() -> None:
    blob = json.dumps(import_entries([entry()], set()).records, ensure_ascii=False)
    for secret in PRIVATE:
        assert secret not in blob


def test_listings_already_saved_or_repeated_in_the_file_are_skipped() -> None:
    result = import_entries([entry(1), entry(1), entry(2)], {"2"})
    assert [r["id"] for r in result.records] == [1] and result.duplicates == 2


def test_unapproved_listings_and_bad_entries_are_rejected_without_stopping_the_rest() -> None:
    bad = [
        entry(10, status="rejected"),                                   # not an approved listing
        {"id": "11", "url": "https://evil.example/x-11", "details": details(11)},   # not a Wasalt URL
        {"id": "12", "url": "https://wasalt.sa/en/property/sale/x-12", "details": {"no": "id"}},
        {"id": "13", "url": "https://wasalt.sa/en/property/sale/x-13", "details": "not a dict"},
        "garbage",
        entry(14),                                                      # the one good entry
    ]
    result = import_entries(bad, set())
    assert [r["id"] for r in result.records] == [14]
    assert len(result.rejected) == 5
    assert import_entries("not a list", set()).rejected == ["file is not a list of entries"]
    assert import_entries({"a": entry(20)}, set()).records[0]["id"] == 20   # a keyed object also works


def test_listing_url_check() -> None:
    assert LISTING_URL_RE.match("https://wasalt.sa/en/property/rent/apartment-with-3-bedrooms-5841667")
    assert not LISTING_URL_RE.match("https://wasalt.sa/en/search?x=1")
    assert not LISTING_URL_RE.match("https://wasalt.sa.evil.example/en/property/sale/x-1")
    assert not LISTING_URL_RE.match("http://wasalt.sa/en/property/sale/x-1")


def test_import_command_appends_and_reports(tmp_path: Path, capsys) -> None:
    out = tmp_path / "wasalt_listings.jsonl"
    out.write_text(json.dumps({"id": 1}) + "\n", encoding="utf-8")
    source = tmp_path / "wasalt-collected.json"
    source.write_text(json.dumps([entry(1), entry(2), entry(3)]), encoding="utf-8")
    import_main([str(source), "--out", str(out)])
    assert saved_ids(out) == {"1", "2", "3"}
    assert "imported 2" in capsys.readouterr().out
    import_main([str(source), "--out", str(out)])                       # a second run adds nothing
    assert saved_ids(out) == {"1", "2", "3"} and "imported 0" in capsys.readouterr().out


def test_pick_urls_balances_sale_and_rent_and_skips_saved_listings() -> None:
    urls = ([f"https://wasalt.sa/en/property/sale/apartment-{i}" for i in range(1000, 1400)]
            + [f"https://wasalt.sa/en/property/rent/apartment-{i}" for i in range(2000, 2200)])
    seen = {"1000", "1010", "2000"}
    picked = pick_urls(urls, 30, 20, seen)
    assert sum("/sale/" in u for u in picked) == 30 and sum("/rent/" in u for u in picked) == 20
    assert len(set(picked)) == 50
    assert not {u.rsplit("-", 1)[-1] for u in picked} & seen
    assert pick_urls(urls, 5, 0, set()) == pick_urls(urls, 5, 0, set())  # deterministic


def test_the_open_page_lists_every_link_safely() -> None:
    page = render_open_page(["https://wasalt.sa/en/property/sale/apartment-with-3-bedrooms-5841667",
                             "https://wasalt.sa/en/property/rent/villa-<script>-1"])
    assert page.count("<li>") == 2 and 'target="_blank"' in page and "noopener" in page
    assert "<script>" not in page.split("<ol>")[1]                      # titles are escaped
