"""Small text normalisers shared by ingestion and the agent tools."""

from __future__ import annotations

import re

# Pages and users write the same country several ways; store and search one spelling.
COUNTRY_ALIASES = {
    "ksa": "Saudi Arabia",
    "saudi": "Saudi Arabia",
    "kingdom of saudi arabia": "Saudi Arabia",
    "united arab emirates": "UAE",
    "u.a.e.": "UAE",
    "u.a.e": "UAE",
    "emirates": "UAE",
    "dubai": "UAE",
    "uk": "United Kingdom",
    "u.k.": "United Kingdom",
    "britain": "United Kingdom",
    "great britain": "United Kingdom",
    "england": "United Kingdom",
    "sultanate of oman": "Oman",
}


def canonical_country(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    return COUNTRY_ALIASES.get(cleaned.lower(), cleaned) or None
