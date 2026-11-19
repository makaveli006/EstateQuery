"""Tools the agent can call. All of them are read-only queries over the SQLite database.

`TOOL_SPECS` is provider-neutral (name, description, JSON Schema); each provider adapter
converts it to its own wire format. `run_tool` never raises: bad input comes back as an
`{"error": ...}` result so the model can correct itself.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from statistics import median
from typing import Any, Callable
from urllib.parse import urlparse

from estatequery.agent.validation import validate_args
from estatequery.config import get_settings
from estatequery.normalize import canonical_country
from estatequery.retrieval.search import ranked_ids

WASALT_SOURCE = "Wasalt.sa"
MAX_LISTINGS = 8
MAX_PASSAGES = 6
# A project list longer than this is returned as a compact view. It sits below the agent loop's
# cut-off for tool results (loop.MAX_TOOL_RESULT_CHARS), so a list is never cut off partway.
LIST_BUDGET_CHARS = 6000
STOPWORDS = {
    "a", "an", "the", "in", "on", "at", "of", "for", "to", "and", "or", "with", "is", "are",
    "what", "which", "who", "how", "does", "do", "about", "tell", "me", "any", "there", "by",
    "from", "that", "this", "it", "has", "have", "can", "i", "you", "your", "my",
}

LISTING_COLUMNS = (
    "id, title, purpose, property_type, city, district, price, currency, rent_frequency, "
    "bedrooms, bathrooms, area_sqm, price_per_sqm, url"
)
SORTS = {
    "price_asc": "price ASC",
    "price_desc": "price DESC",
    "newest": "published_at DESC",
    "area_desc": "area_sqm DESC",
    "price_per_sqm_asc": "price_per_sqm ASC",
}
GROUPS = {"city": "city", "district": "district", "property_type": "property_type", "bedrooms": "bedrooms"}


# --------------------------------------------------------------------------- helpers


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or get_settings().db_path)
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def norm(value: str) -> str:
    """Lower-case and loosen punctuation so 'Al Malqa' matches 'Al-Malqa'."""
    return re.sub(r"\s+", " ", re.sub(r"[-_'’`]", " ", value.lower())).strip()


def like_clause(column: str) -> str:
    return f"REPLACE(REPLACE(LOWER({column}), '-', ' '), '''', ' ') LIKE ?"


def fts_query(text: str, joiner: str) -> str | None:
    tokens = [t for t in re.findall(r"\w+", text.lower()) if t not in STOPWORDS and len(t) > 1]
    if not tokens:
        return None
    return f" {joiner} ".join(f'"{t}"' for t in tokens[:12])


def clean_number(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    """Drop empty values and turn 3.0 into 3 so results are compact and readable."""
    out: dict[str, Any] = {}
    for key in row.keys():
        value = row[key]
        if value is None or value == "":
            continue
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        out[key] = value
    return out


def snapshot_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key='snapshot_date'").fetchone()
    return row["value"] if row else None


def listing_filters(args: dict[str, Any]) -> tuple[list[str], list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    if args.get("purpose"):
        where.append("purpose = ?")
        params.append(str(args["purpose"]).lower())
    for field in ("city", "district", "property_type"):
        if args.get(field):
            where.append(like_clause(field))
            params.append(f"%{norm(str(args[field]))}%")
    ranges = (
        ("min_price", "price >= ?"), ("max_price", "price <= ?"),
        ("min_bedrooms", "bedrooms >= ?"), ("max_bedrooms", "bedrooms <= ?"),
        ("min_area_sqm", "area_sqm >= ?"), ("max_area_sqm", "area_sqm <= ?"),
    )
    for key, clause in ranges:
        if args.get(key) is not None:
            where.append(clause)
            params.append(float(args[key]))
    return where, params


def facets(conn: sqlite3.Connection, city: str | None) -> dict[str, Any]:
    """Valid filter values, returned when a search finds nothing so the model can retry."""
    out: dict[str, Any] = {
        "cities": [r[0] for r in conn.execute(
            "SELECT city FROM listings WHERE city IS NOT NULL GROUP BY city ORDER BY COUNT(*) DESC LIMIT 15")],
        "property_types": [r[0] for r in conn.execute(
            "SELECT property_type FROM listings WHERE property_type IS NOT NULL "
            "GROUP BY property_type ORDER BY COUNT(*) DESC LIMIT 15")],
    }
    if city:
        out["districts_in_city"] = [r[0] for r in conn.execute(
            f"SELECT district FROM listings WHERE {like_clause('city')} AND district IS NOT NULL "
            "GROUP BY district ORDER BY COUNT(*) DESC LIMIT 25", (f"%{norm(city)}%",))]
    return out


# --------------------------------------------------------------------------- tools


def search_listings(conn: sqlite3.Connection, args: dict[str, Any]) -> dict[str, Any]:
    where, params = listing_filters(args)
    if args.get("keywords"):
        match = fts_query(str(args["keywords"]), "OR")
        if match:
            where.append("id IN (SELECT rowid FROM listings_fts WHERE listings_fts MATCH ?)")
            params.append(match)
    sort = SORTS.get(str(args.get("sort") or ""), "published_at DESC")
    sort_column = sort.split()[0]
    limit = max(1, min(int(args.get("limit") or 5), MAX_LISTINGS))
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    total = conn.execute(f"SELECT COUNT(*) FROM listings{clause}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT {LISTING_COLUMNS} FROM listings{clause} "
        f"ORDER BY {sort_column} IS NULL, {sort} LIMIT ?",
        [*params, limit],
    ).fetchall()
    result: dict[str, Any] = {
        "total_matches": total,
        "returned": len(rows),
        "listings": [clean_number(r) for r in rows],
        "source": WASALT_SOURCE,
        "snapshot_date": snapshot_date(conn),
    }
    if total == 0:
        result["hint"] = "No listings match. Valid filter values in this dataset are listed under 'available'."
        result["available"] = facets(conn, args.get("city"))
    return result


def listing_stats(conn: sqlite3.Connection, args: dict[str, Any]) -> dict[str, Any]:
    group_by = GROUPS.get(str(args.get("group_by") or ""))
    if not group_by:
        return {"error": f"group_by must be one of {sorted(GROUPS)}"}
    where, params = listing_filters(args)
    where += ["price IS NOT NULL", f"{group_by} IS NOT NULL"]
    rows = conn.execute(
        f"SELECT {group_by} AS grp, price, price_per_sqm FROM listings WHERE " + " AND ".join(where),
        params,
    ).fetchall()
    if not rows:
        return {"total_listings": 0, "groups": [], "hint": "No priced listings match.",
                "available": facets(conn, args.get("city"))}

    buckets: dict[Any, dict[str, list[float]]] = {}
    for r in rows:
        bucket = buckets.setdefault(r["grp"], {"price": [], "ppsqm": []})
        bucket["price"].append(r["price"])
        if r["price_per_sqm"]:
            bucket["ppsqm"].append(r["price_per_sqm"])

    groups = []
    for name, bucket in buckets.items():
        prices = bucket["price"]
        entry: dict[str, Any] = {
            "group": int(name) if isinstance(name, float) and name.is_integer() else name,
            "count": len(prices),
            "median_price": round(median(prices)),
            "min_price": round(min(prices)),
            "max_price": round(max(prices)),
        }
        if bucket["ppsqm"]:
            entry["median_price_per_sqm"] = round(median(bucket["ppsqm"]))
        groups.append(entry)
    groups.sort(key=lambda g: -g["count"])
    all_prices = [r["price"] for r in rows]
    return {
        "total_listings": len(rows),
        "overall_median_price": round(median(all_prices)),
        "currency": "SAR",
        "groups": groups[:15],
        "note": "Statistics describe the sampled snapshot, not the whole market. "
                "Rent prices follow each listing's rent frequency.",
        "source": WASALT_SOURCE,
        "snapshot_date": snapshot_date(conn),
    }


def get_listing(conn: sqlite3.Connection, args: dict[str, Any]) -> dict[str, Any]:
    try:
        listing_id = int(args.get("id"))
    except (TypeError, ValueError):
        return {"error": "id must be the numeric listing id returned by search_listings"}
    row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    if not row:
        return {"error": f"No listing with id {listing_id} in this dataset."}
    record = clean_number(row)
    extras = json.loads(record.pop("extras_json", "{}") or "{}")
    if extras:
        record["extras"] = extras
    if record.get("description"):
        record["description"] = record["description"][:1200]
    record["source"] = WASALT_SOURCE
    record["snapshot_date"] = snapshot_date(conn)
    return record


NO_PARTNER = "(no partner)"


def group_by_brand(projects: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group projects under their brand partner: name, country and link only.

    Named brands come first in alphabetical order, projects without a partner last.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for project in projects:
        item: dict[str, Any] = {"name": project.get("name")}
        if project.get("country"):
            item["country"] = project["country"]
        item["url"] = project.get("url")
        groups.setdefault(project.get("brand_partner") or NO_PARTNER, []).append(item)
    return dict(sorted(groups.items(), key=lambda entry: (entry[0] == NO_PARTNER, entry[0].lower())))


def compact_project_view(projects: list[dict[str, Any]]) -> dict[str, Any]:
    """Every project grouped by brand, small enough to pass the agent loop untruncated.

    If even the compact form is over budget (many projects, long names), the country is
    dropped first and then whole projects are left out, with the number omitted stated in the
    result, so the model is never handed a list that stops silently halfway.
    """
    note = ("The full list is long, so this is a compact view: every project, grouped by brand "
            "partner, with its country and link. For status, property types, price or amenities, "
            "filter by country, property_type or brand, or call get_darglobal_project.")
    view: dict[str, Any] = {
        "count": len(projects),
        "view": "compact, grouped by brand partner",
        "by_brand": group_by_brand(projects),
        "note": note,
        "source": "darglobal.co.uk",
    }

    def too_long() -> bool:
        return len(json.dumps(view, ensure_ascii=False)) > LIST_BUDGET_CHARS

    if too_long():
        for items in view["by_brand"].values():
            for item in items:
                item.pop("country", None)
    omitted = 0
    while too_long() and view["by_brand"]:
        largest = max(view["by_brand"].values(), key=len)
        largest.pop()
        omitted += 1
        view["by_brand"] = {brand: items for brand, items in view["by_brand"].items() if items}
        view["omitted"] = omitted
        view["note"] = (f"{note} {omitted} more projects are not shown because the list is long; "
                        "filter by country, property_type or brand to see them.")
    return view


def list_darglobal_projects(conn: sqlite3.Connection, args: dict[str, Any]) -> dict[str, Any]:
    where: list[str] = []
    params: list[Any] = []
    for key, column in (("country", "country"), ("property_type", "property_types"), ("brand", "brand_partner")):
        if args.get(key):
            value = str(args[key])
            if key == "country":
                value = canonical_country(value) or value  # "United Arab Emirates" -> "UAE"
            elif key == "property_type":
                value = value.rstrip("sS")  # "villas" should match "Villa" and "Villas"
            where.append(like_clause(column))
            params.append(f"%{norm(value)}%")
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        "SELECT name, country, city, property_types, brand_partner, status, starting_price, slug, url "
        f"FROM projects{clause} ORDER BY country, name LIMIT 60",
        params,
    ).fetchall()
    if rows:
        projects = [clean_number(r) for r in rows]
        full = {"count": len(rows), "projects": projects, "source": "darglobal.co.uk"}
        if len(json.dumps(full, ensure_ascii=False)) <= LIST_BUDGET_CHARS:
            return full
        # Too long to show in full: the agent loop cuts tool results at a fixed size, and a list
        # cut off partway made answers about the whole portfolio incomplete. A compact view that
        # keeps every project, with its brand, country and link, fits and costs fewer tokens.
        return compact_project_view(projects)

    # No structured facts (or nothing matched): fall back to project page titles.
    filters = [str(args[k]) for k in ("country", "property_type", "brand") if args.get(k)]
    page_where = "page_type = 'project'"
    page_params: list[Any] = []
    for value in filters:
        page_where += f" AND ({like_clause('title')} OR {like_clause('text')})"
        page_params += [f"%{norm(value)}%"] * 2
    pages = conn.execute(
        f"SELECT title AS name, description, slug, url FROM pages WHERE {page_where} ORDER BY title LIMIT 60",
        page_params,
    ).fetchall()
    return {
        "count": len(pages),
        "projects": [clean_number(r) for r in pages],
        "note": "Listed from project page titles; call get_darglobal_project for details.",
        "source": "darglobal.co.uk",
    }


def search_darglobal(conn: sqlite3.Connection, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        return {"error": "query is required"}
    limit = max(1, min(int(args.get("limit") or 4), MAX_PASSAGES))
    settings = get_settings()
    ids, mode = ranked_ids(conn, query, limit, mode=settings.retrieval_mode, rerank=settings.retrieval_rerank,
                           rerank_candidates=settings.rerank_candidates)
    found = {}
    if ids:
        marks = ",".join("?" for _ in ids)
        found = {r["id"]: r for r in conn.execute(
            f"SELECT id, title, heading, text, url FROM chunks WHERE id IN ({marks})", ids)}
    passages = []
    for chunk_id in ids:
        if chunk_id not in found:
            continue
        item = clean_number(found[chunk_id])
        item.pop("id", None)
        item["text"] = item["text"][:800]
        passages.append(item)
    result: dict[str, Any] = {"returned": len(passages), "passages": passages, "source": "darglobal.co.uk"}
    if mode != "bm25":
        result["retrieval"] = mode
    if not passages:
        result["hint"] = "No passages matched. Try different keywords or call list_darglobal_projects."
    return result


def get_darglobal_project(conn: sqlite3.Connection, args: dict[str, Any]) -> dict[str, Any]:
    ref = str(args.get("project") or "").strip()
    if not ref:
        return {"error": "project is required: a slug or name from list_darglobal_projects"}
    slug = ref.strip("/").lower()
    pattern = f"%{norm(ref)}%"
    facts = conn.execute(
        f"SELECT * FROM projects WHERE slug = ? OR {like_clause('name')} LIMIT 1", (slug, pattern)
    ).fetchone()
    page = conn.execute(
        f"SELECT * FROM pages WHERE slug = ? OR slug = ? OR {like_clause('title')} OR {like_clause('slug')} "
        "ORDER BY (slug = ?) DESC, LENGTH(slug) LIMIT 1",
        (slug, facts["slug"] if facts else slug, pattern, pattern, facts["slug"] if facts else slug),
    ).fetchone()
    if not facts and not page:
        return {"error": f"No DarGlobal project matching '{ref}'. Call list_darglobal_projects for valid names."}
    result: dict[str, Any] = {}
    if facts:
        result["facts"] = clean_number(facts)
    if page:
        result["page"] = {"title": page["title"], "url": page["url"], "text": (page["text"] or "")[:3500]}
    result["source"] = "darglobal.co.uk"
    return result


# --------------------------------------------------------------------------- registry

NUMBER = {"type": "number"}
LISTING_FILTER_PROPS: dict[str, Any] = {
    "purpose": {"type": "string", "enum": ["sale", "rent"], "description": "Buy or rent."},
    "city": {"type": "string", "description": "City in Saudi Arabia, e.g. Riyadh, Jeddah."},
    "district": {"type": "string", "description": "District / neighbourhood name."},
    "property_type": {"type": "string", "description": "e.g. Apartment, Villa, Land, Floor, Building."},
    "min_price": {**NUMBER, "description": "Minimum price in SAR."},
    "max_price": {**NUMBER, "description": "Maximum price in SAR."},
    "min_bedrooms": {**NUMBER, "description": "Minimum number of bedrooms."},
    "max_bedrooms": {**NUMBER, "description": "Maximum number of bedrooms."},
    "min_area_sqm": {**NUMBER, "description": "Minimum area in square metres."},
    "max_area_sqm": {**NUMBER, "description": "Maximum area in square metres."},
}

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "search_listings",
        "description": (
            "Search Wasalt property listings in Saudi Arabia (for sale or rent) with structured filters. "
            "Returns up to 8 listings plus the total number of matches. Use this for any question about "
            "specific properties, availability, or 'find me ...' requests."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                **LISTING_FILTER_PROPS,
                "keywords": {"type": "string", "description": "Free-text words to match in title/description."},
                "sort": {"type": "string", "enum": sorted(SORTS), "description": "Result order. Default newest."},
                "limit": {"type": "integer", "description": "How many listings to return (1-8, default 5)."},
            },
            "required": [],
        },
    },
    {
        "name": "listing_stats",
        "description": (
            "Aggregate Wasalt listings: count and median/min/max price (and median price per sqm for sales) "
            "grouped by city, district, property_type or bedrooms. Use for averages, comparisons and "
            "'how much does X cost in Y' questions. Always set purpose so sale and rent prices are not mixed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "group_by": {"type": "string", "enum": sorted(GROUPS)},
                **LISTING_FILTER_PROPS,
            },
            "required": ["group_by"],
        },
    },
    {
        "name": "get_listing",
        "description": "Full details of one Wasalt listing by its numeric id, including description and extras.",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "Listing id from search_listings."}},
            "required": ["id"],
        },
    },
    {
        "name": "list_darglobal_projects",
        "description": (
            "List DarGlobal development projects, optionally filtered by country, property type or brand "
            "partner. Use for overview questions such as 'what projects does DarGlobal have in Oman' or "
            "'which brands does DarGlobal partner with'. A long list (for example with no filter) comes "
            "back as a compact view grouped by brand partner, with each project's country and link."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "country": {"type": "string", "description": "e.g. UAE, Oman, Saudi Arabia, Qatar, Spain, UK."},
                "property_type": {"type": "string", "description": "e.g. villas, apartments, hotel rooms."},
                "brand": {"type": "string", "description": "Brand partner, e.g. Trump, Missoni, Pagani."},
            },
            "required": [],
        },
    },
    {
        "name": "search_darglobal",
        "description": (
            "Full-text search over DarGlobal website content (projects, partners, company and FAQ pages). "
            "Returns matching passages with their page URL. Use short keyword queries."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords, e.g. 'Aida Oman golf villas amenities'."},
                "limit": {"type": "integer", "description": "Passages to return (1-6, default 4)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_darglobal_project",
        "description": "Details of one DarGlobal project: structured facts and the page text.",
        "parameters": {
            "type": "object",
            "properties": {"project": {"type": "string", "description": "Project slug or name."}},
            "required": ["project"],
        },
    },
]

_HANDLERS: dict[str, Callable[[sqlite3.Connection, dict[str, Any]], dict[str, Any]]] = {
    "search_listings": search_listings,
    "listing_stats": listing_stats,
    "get_listing": get_listing,
    "list_darglobal_projects": list_darglobal_projects,
    "search_darglobal": search_darglobal,
    "get_darglobal_project": get_darglobal_project,
}


def run_tool(name: str, args: dict[str, Any] | None, db_path: Path | None = None) -> dict[str, Any]:
    """Execute one tool call. Always returns a JSON-serialisable dict."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return {"error": f"Unknown tool '{name}'. Available: {sorted(_HANDLERS)}"}
    if not isinstance(args, dict):
        return {"error": "Tool arguments must be a JSON object."}
    args, problem = validate_args(name, args)
    if problem is not None:
        return problem
    try:
        conn = connect(db_path)
    except sqlite3.Error as exc:
        return {"error": f"Database unavailable: {exc}"}
    try:
        return handler(conn, args)
    except (sqlite3.Error, ValueError, TypeError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        conn.close()


MAX_SOURCE_LABEL = 60


def source_label(title: Any, url: str) -> str:
    """A short, readable name for a source link.

    Page titles carry a site suffix ("Trump Branded Residences Gulf | DarGlobal Portfolio")
    and some pages have no title at all, which used to put a raw URL in the sources line.
    """
    text = re.sub(r"\s+", " ", str(title or "")).strip()
    if text.lower().startswith(("http://", "https://", "www.")):
        text = ""
    if " | " in text:
        text = text.split(" | ")[0].strip()
    if not text:
        parsed = urlparse(url)
        slug = parsed.path.strip("/").rsplit("/", 1)[-1]
        slug = re.sub(r"-\d{4,}$", "", slug)  # trailing listing id
        text = re.sub(r"[-_]+", " ", slug).strip().title() or parsed.netloc.removeprefix("www.")
    if len(text) > MAX_SOURCE_LABEL:
        text = text[: MAX_SOURCE_LABEL - 1].rstrip() + "…"
    return text


def collect_sources(name: str, result: dict[str, Any]) -> list[dict[str, str]]:
    """Pull citeable links out of a tool result for the UI's sources list."""
    found: list[dict[str, str]] = []

    def add(title: Any, url: Any) -> None:
        if url:
            found.append({"title": source_label(title, str(url)), "url": str(url)})

    for item in result.get("listings") or []:
        add(item.get("title"), item.get("url"))
    for item in result.get("passages") or []:
        add(item.get("title"), item.get("url"))
    for item in result.get("projects") or []:
        add(item.get("name"), item.get("url"))
    for group in (result.get("by_brand") or {}).values():  # the compact project view
        for item in group:
            add(item.get("name"), item.get("url"))
    if name == "get_listing":
        add(result.get("title"), result.get("url"))
    if name == "get_darglobal_project":
        page = result.get("page") or {}
        add(page.get("title"), page.get("url") or (result.get("facts") or {}).get("url"))
    return found
