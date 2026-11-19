"""Anonymous visitor identity for chat history.

The demo has no accounts. To give every visitor their own private conversation history
without a login screen, the server hands each browser a random, unguessable id in an
HttpOnly cookie, and the chat UI signs that browser in as a guest keyed by the id.

This is identification, not authentication: whoever holds the cookie sees that history.
That is the right trade for a public demo, and it means clearing cookies starts afresh.
"""

from __future__ import annotations

import re
import uuid
from http.cookies import SimpleCookie

VISITOR_COOKIE = "eq_visitor"
COOKIE_MAX_AGE = 180 * 24 * 3600  # six months
_VALID = re.compile(r"^[0-9a-f]{32}$")


def new_visitor_id() -> str:
    return uuid.uuid4().hex


def visitor_from_cookie_header(cookie_header: str | None) -> str | None:
    """Return the visitor id from a raw Cookie header, or None if absent or malformed."""
    if not cookie_header:
        return None
    try:
        jar = SimpleCookie()
        jar.load(cookie_header)
    except Exception:  # noqa: BLE001 - a malformed header is the same as no cookie
        return None
    morsel = jar.get(VISITOR_COOKIE)
    value = morsel.value if morsel else None
    return value if value and _VALID.match(value) else None


def guest_identifier(visitor_id: str) -> str:
    return f"guest-{visitor_id[:16]}"
