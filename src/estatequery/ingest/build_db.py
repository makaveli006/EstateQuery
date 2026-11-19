"""Build the read-only SQLite database the chatbot answers from.

Inputs (JSON Lines, in `<data_dir>/raw/`):
  wasalt_listings.jsonl     one record per listing, as written by scrape/wasalt.py
  darglobal_pages.jsonl     one record per page: {url, title, description, text, headings?}
  darglobal_projects.jsonl  optional structured facts per project page (ingest/enrich.py)

Output: `<data_dir>/estatequery.db` with structured tables plus FTS5 full-text indexes.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from estatequery.config import get_settings
from estatequery.normalize import canonical_country

SCHEMA = """
CREATE TABLE listings (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT,
    purpose TEXT,            -- 'sale' | 'rent'
    property_type TEXT,      -- Apartment, Villa, Land, ...
    main_type TEXT,          -- Residential | Commercial
    city TEXT,
    district TEXT,
    territory TEXT,
    address TEXT,
    price REAL,              -- SAR; for rent see rent_frequency
    currency TEXT,
    rent_frequency TEXT,
    bedrooms REAL,
    bathrooms REAL,
    living_rooms REAL,
    area_sqm REAL,
    price_per_sqm REAL,
    furnishing TEXT,
    age TEXT,
    facade TEXT,
    extras_json TEXT,
    description TEXT,
    lat REAL,
    lon REAL,
    published_at TEXT,
    updated_at TEXT
);
CREATE INDEX idx_listings_city ON listings(city COLLATE NOCASE);
CREATE INDEX idx_listings_district ON listings(district COLLATE NOCASE);
CREATE INDEX idx_listings_purpose ON listings(purpose);
CREATE INDEX idx_listings_type ON listings(property_type COLLATE NOCASE);
CREATE INDEX idx_listings_price ON listings(price);
CREATE INDEX idx_listings_bedrooms ON listings(bedrooms);
CREATE VIRTUAL TABLE listings_fts USING fts5(
    title, description, district, city, property_type,
    content='listings', content_rowid='id', tokenize='porter unicode61'
);

CREATE TABLE pages (
    slug TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT,
    description TEXT,
    page_type TEXT,          -- project | collection | partner | company
    text TEXT
);

CREATE TABLE projects (
    slug TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    name TEXT,
    country TEXT,
    city TEXT,
    property_types TEXT,     -- comma separated
    brand_partner TEXT,
    status TEXT,
    starting_price TEXT,     -- as stated on the page, with currency
    bedrooms TEXT,
    amenities TEXT,          -- comma separated
    summary TEXT
);

CREATE TABLE chunks (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT,
    heading TEXT,
    text TEXT NOT NULL
);
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    title, heading, text,
    content='chunks', content_rowid='id', tokenize='porter unicode61'
);

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""

COLLECTION_PREFIXES = ("projects",)
PARTNER_PREFIXES = ("partners",)
COMPANY_SLUGS = {
    "", "about", "faq", "why-invest", "investor", "hospitality", "commercial",
    "development-management", "dg-circle", "exclusive-membership", "insignia",
    "tokenization", "luxury-golf-communities", "live-all-in",
}
CHUNK_CHARS = 1000
COOKIE_MARKERS = ("We value our privacy", "We use cookies to enhance your browsing experience")


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def tidy(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def slug_from_url(url: str) -> str:
    return urlparse(url).path.strip("/").lower()


def page_type_for(slug: str) -> str:
    first = slug.split("/", 1)[0]
    if first in COLLECTION_PREFIXES:
        return "collection"
    if first in PARTNER_PREFIXES:
        return "partner"
    if slug in COMPANY_SLUGS:
        return "company"
    return "project"


def chunk_text(text: str, headings: list[str] | None = None, size: int = CHUNK_CHARS) -> list[tuple[str | None, str]]:
    """Split page text into (heading, passage) pairs of roughly `size` characters.

    Lines that match a known heading start a new passage; long passages are split on
    line boundaries so a passage never cuts a sentence in half where avoidable.
    """
    heading_set = {h.strip().lower() for h in (headings or []) if h and h.strip()}
    passages: list[tuple[str | None, str]] = []
    current_heading: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        body = " ".join(buffer).strip()
        if len(body) >= 40:
            passages.append((current_heading, body))
        buffer.clear()

    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        if line.lower() in heading_set and len(line) <= 120:
            flush()
            current_heading = line
            continue
        if sum(len(part) + 1 for part in buffer) + len(line) > size and buffer:
            flush()
        while len(line) > size:  # a single very long line
            cut = line.rfind(". ", 0, size)
            cut = cut + 1 if cut > size // 2 else size
            buffer.append(line[:cut])
            flush()
            line = line[cut:].strip()
        buffer.append(line)
    flush()
    return passages


def strip_boilerplate(text: str) -> str:
    """Drop the cookie-consent banner that the site appends to every page."""
    for marker in COOKIE_MARKERS:
        index = text.find(marker)
        if index > 0:
            text = text[:index]
    return text.strip()


def as_list_text(value: Any) -> str | None:
    if isinstance(value, list):
        items = [tidy(v) for v in value]
        return ", ".join(i for i in items if i) or None
    return tidy(value)


def load_listings(conn: sqlite3.Connection, path: Path) -> int:
    count = 0
    for r in read_jsonl(path):
        if r.get("id") is None or not r.get("url"):
            continue
        price = positive(r.get("price"))
        area = positive(r.get("area_sqm"))
        purpose = (r.get("purpose") or "").lower() or None
        per_sqm = round(price / area, 2) if price and area and purpose == "sale" else None
        extras = r.get("extras") or {}
        conn.execute(
            """INSERT OR REPLACE INTO listings VALUES
               (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                int(r["id"]), r["url"], tidy(r.get("title")), purpose,
                tidy(r.get("property_type")), tidy(r.get("main_type")),
                tidy(r.get("city")), tidy(r.get("district")), tidy(r.get("territory")),
                tidy(r.get("address")), price, r.get("currency") or "SAR",
                tidy(r.get("rent_frequency")), positive(r.get("bedrooms")),
                positive(r.get("bathrooms")), positive(r.get("living_rooms")), area, per_sqm,
                tidy(r.get("furnishing")), tidy(extras.get("age")), tidy(extras.get("facade")),
                json.dumps(extras, ensure_ascii=False), tidy(r.get("description")),
                r.get("lat"), r.get("lon"), r.get("published_at"), r.get("updated_at"),
            ),
        )
        count += 1
    conn.execute("INSERT INTO listings_fts(listings_fts) VALUES('rebuild')")
    return count


def load_pages(conn: sqlite3.Connection, path: Path) -> tuple[int, int]:
    pages = chunks = 0
    for r in read_jsonl(path):
        url = r.get("url")
        text = strip_boilerplate((r.get("text") or "").strip())
        if not url or len(text) < 80:
            continue
        slug = slug_from_url(url)
        title = tidy(r.get("title"))
        conn.execute(
            "INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?,?)",
            (slug, url, title, tidy(r.get("description")), page_type_for(slug), text),
        )
        pages += 1
        for heading, passage in chunk_text(text, r.get("headings")):
            conn.execute(
                "INSERT INTO chunks(slug, url, title, heading, text) VALUES (?,?,?,?,?)",
                (slug, url, title, heading, passage),
            )
            chunks += 1
    conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
    return pages, chunks


def load_projects(conn: sqlite3.Connection, path: Path) -> int:
    count = 0
    for r in read_jsonl(path):
        url = r.get("url")
        if not url or not r.get("name"):
            continue
        conn.execute(
            "INSERT OR REPLACE INTO projects VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                slug_from_url(url), url, tidy(r.get("name")), canonical_country(tidy(r.get("country"))),
                tidy(r.get("city")), as_list_text(r.get("property_types")),
                tidy(r.get("brand_partner")), tidy(r.get("status")),
                tidy(r.get("starting_price")), tidy(r.get("bedrooms")),
                as_list_text(r.get("amenities")), tidy(r.get("summary")),
            ),
        )
        count += 1
    return count


def build(raw_dir: Path, db_path: Path, snapshot: str | None = None) -> dict[str, int]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        listings = load_listings(conn, raw_dir / "wasalt_listings.jsonl")
        pages, chunks = load_pages(conn, raw_dir / "darglobal_pages.jsonl")
        projects = load_projects(conn, raw_dir / "darglobal_projects.jsonl")
        stats = {"listings": listings, "pages": pages, "chunks": chunks, "projects": projects}
        meta = {"snapshot_date": snapshot or date.today().isoformat(), **{k: str(v) for k, v in stats.items()}}
        conn.executemany("INSERT INTO meta VALUES (?,?)", meta.items())
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()
    return stats


def main(argv: list[str] | None = None) -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Build the EstateQuery SQLite database.")
    parser.add_argument("--raw-dir", type=Path, default=settings.raw_dir)
    parser.add_argument("--db", type=Path, default=settings.db_path)
    parser.add_argument("--snapshot", default=None, help="snapshot date (YYYY-MM-DD), default today")
    args = parser.parse_args(argv)
    stats = build(args.raw_dir, args.db, args.snapshot)
    print(f"built {args.db}: " + ", ".join(f"{v} {k}" for k, v in stats.items()))


if __name__ == "__main__":
    main()
