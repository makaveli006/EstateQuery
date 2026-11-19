from __future__ import annotations

import json

from estatequery.scrape.wasalt import clean_text, parse_listing, stride_sample

# Shape mirrors a real listing page; every value is invented.
NEXT_DATA = {
    "props": {
        "pageProps": {
            "propertyDetailsV3": {
                "id": 123456,
                "leadContactInfo": {"ownerName": "PRIVATE OWNER NAME"},
                "propertyOwner": {"resEmployee": "PRIVATE EMPLOYEE", "falLicenseNumber": "9999000011"},
                "regaVerifiedInfo": [{"fields": [{"key": "deedNumber", "value": "1111222233334444"}]}],
                "location": {"lat": "24.7", "lon": "46.6"},
                "attributes": [
                    {"key": "noOfBedrooms", "value": 3},
                    {"key": "noOfBathrooms", "value": "2"},
                    {"key": "builtUpArea", "value": "133", "unit": "SQM"},
                ],
                "additionalAttributes": [
                    {"key": "completionYear", "value": "New"},
                    {"key": "landNumber", "value": "3991"},
                    {"key": "street", "value": "PRIVATE STREET NAME"},
                ],
                "publishedAt": "2026-01-28T07:49:47.523Z",
                "updatedAt": "2026-01-28T13:45:27.565Z",
                "propertyInfo": {
                    "title": "Apartment with 3 Bedrooms",
                    "propertyFor": "sale",
                    "propertySubType": "Apartment",
                    "propertyMainType": "Residential",
                    "city": "Riyadh",
                    "district": "Al-Mahdiyah",
                    "territory": "West Riyadh",
                    "address": "Al-Mahdiyah, West Riyadh, Riyadh",
                    "salePrice": 850000,
                    "expectedRent": "",
                    "currencyType": "SAR",
                    "statusTypeSlug": "approved",
                    "description": "<p>Nice flat.</p><p>Call 0551234567 or ٠٥٥١٢٣٤٥٦٧ now</p>",
                },
            }
        }
    }
}


def page(data: dict) -> str:
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


def test_parse_listing_keeps_property_fields() -> None:
    record = parse_listing(page(NEXT_DATA), "https://wasalt.sa/en/property/sale/x-123456")
    assert record is not None
    assert record["id"] == 123456
    assert record["purpose"] == "sale"
    assert record["price"] == 850000
    assert record["bedrooms"] == 3
    assert record["bathrooms"] == 2
    assert record["area_sqm"] == 133
    assert record["city"] == "Riyadh" and record["district"] == "Al-Mahdiyah"
    assert record["extras"] == {"age": "New"}


def test_parse_listing_drops_personal_and_licence_data() -> None:
    record = parse_listing(page(NEXT_DATA), "https://wasalt.sa/en/property/sale/x-123456")
    blob = json.dumps(record, ensure_ascii=False)
    for secret in ("PRIVATE OWNER NAME", "PRIVATE EMPLOYEE", "9999000011", "1111222233334444",
                   "PRIVATE STREET NAME", "3991", "0551234567", "٠٥٥١٢٣٤٥٦٧"):
        assert secret not in blob
    assert "[contact removed]" in record["description"]


def test_parse_listing_rejects_unapproved_and_empty_pages() -> None:
    data = json.loads(json.dumps(NEXT_DATA))
    data["props"]["pageProps"]["propertyDetailsV3"]["propertyInfo"]["statusTypeSlug"] = "rejected"
    assert parse_listing(page(data), "u") is None
    assert parse_listing("<html>no data</html>", "u") is None


def test_rent_listing_uses_rent_price() -> None:
    data = json.loads(json.dumps(NEXT_DATA))
    info = data["props"]["pageProps"]["propertyDetailsV3"]["propertyInfo"]
    info.update({"propertyFor": "rent", "salePrice": None, "expectedRent": "60000", "rentFreq": "yearly"})
    record = parse_listing(page(data), "u")
    assert record["price"] == 60000 and record["rent_frequency"] == "yearly"

    # The live site sends a map of rent periods; the default one wins.
    info.update({"expectedRent": "", "rentFreq": {
        "monthly": {"freq": "monthly", "amount": 4000},
        "yearly": {"freq": "yearly", "amount": 38000, "default_freq": True},
    }})
    record = parse_listing(page(data), "u")
    assert record["price"] == 38000 and record["rent_frequency"] == "yearly"


def test_clean_text_and_stride_sample() -> None:
    assert clean_text("<b>Hello</b>&amp; bye") == "Hello & bye"
    assert clean_text("") is None
    assert stride_sample(list("abcdefghij"), 5) == ["a", "c", "e", "g", "i"]
    assert stride_sample(["a"], 5) == ["a"]
