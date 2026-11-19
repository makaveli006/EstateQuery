from __future__ import annotations

from estatequery.ui.identity import VISITOR_COOKIE, guest_identifier, new_visitor_id, visitor_from_cookie_header


def test_visitor_id_round_trips_through_a_cookie_header() -> None:
    visitor = new_visitor_id()
    header = f"theme=dark; {VISITOR_COOKIE}={visitor}; other=1"
    assert visitor_from_cookie_header(header) == visitor


def test_missing_or_malformed_cookies_are_rejected() -> None:
    assert visitor_from_cookie_header(None) is None
    assert visitor_from_cookie_header("") is None
    assert visitor_from_cookie_header("theme=dark") is None
    # Wrong shape: not 32 hex characters, so it cannot be used to pick someone's history.
    assert visitor_from_cookie_header(f"{VISITOR_COOKIE}=admin") is None
    assert visitor_from_cookie_header(f"{VISITOR_COOKIE}=" + "g" * 32) is None
    assert visitor_from_cookie_header(f"{VISITOR_COOKIE}=" + "a" * 31) is None


def test_ids_are_unique_and_guest_names_do_not_expose_the_full_cookie() -> None:
    first, second = new_visitor_id(), new_visitor_id()
    assert first != second
    assert guest_identifier(first) != guest_identifier(second)
    assert first not in guest_identifier(first)
    assert guest_identifier(first).startswith("guest-")
