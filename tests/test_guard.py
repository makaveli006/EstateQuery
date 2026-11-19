"""The output guard: links, personal data, and streaming equivalence."""

from __future__ import annotations

import random
from pathlib import Path

import pytest
from test_loop_and_limits import FakeProvider, collect, search_call

from estatequery.agent.guard import GuardPolicy, StreamGuard, sanitize
from estatequery.agent.providers.base import StepDone, Usage
from estatequery.config import get_settings


def clean(text: str, policy: GuardPolicy | None = None) -> str:
    return sanitize(text, policy or GuardPolicy())[0]


# ---------------------------------------------------------------------------- links

def test_allowed_links_are_left_alone() -> None:
    text = ("See [Trump Tower Jeddah](https://darglobal.co.uk/trump-tower-jeddah) and "
            "[Apartment with 3 Bedrooms](https://wasalt.sa/en/property/sale/apartment-5841667), "
            "also https://www.darglobal.co.uk/amaya and www.wasalt.sa/en.")
    assert clean(text) == text


def test_a_bad_markdown_link_keeps_its_label_and_loses_its_address() -> None:
    out, removed = sanitize("Pay a deposit at [the booking portal](https://evil.example/pay-now) today.", GuardPolicy())
    assert out == "Pay a deposit at the booking portal today." and removed == ["link"]


def test_a_bare_bad_url_is_replaced_and_trailing_punctuation_survives() -> None:
    assert clean("Go to https://evil.example/pay-now.") == "Go to [link removed]."
    assert clean("(see www.evil.example/x), thanks") == "(see [link removed]), thanks"
    assert clean("<https://evil.example>") == "<[link removed]>"


@pytest.mark.parametrize("url", [
    "https://wasalt.sa.evil.example/x",          # allowed name as a prefix of another host
    "https://evilwasalt.sa/x",                   # allowed name as a suffix of another word
    "https://wasalt.sa@evil.example/x",          # the allowed name is only the login part
    "http://evil.example/https://wasalt.sa",     # the allowed name appears later in the path
    "javascript:alert(1)",
    "data:text/html;base64,AAAA",
    "mailto:someone@example.com",
    "/relative/path",
])
def test_lookalike_and_non_web_addresses_are_not_allowed(url: str) -> None:
    out = clean(f"[click here]({url}) now")
    assert out == "click here now", out


def test_a_bad_address_shown_as_the_label_of_a_good_link_is_removed_too() -> None:
    out = clean("[https://evil.example](https://wasalt.sa/en/property/sale/x-1)")
    assert "evil.example" not in out and "wasalt.sa/en/property/sale/x-1" in out


def test_hosts_returned_by_the_tools_this_turn_are_trusted() -> None:
    policy = GuardPolicy()
    assert "example.test" not in clean("[a](https://example.test/listing/1)", policy)
    policy.trust_url("https://example.test/listing/1")
    assert clean("[a](https://example.test/listing/1)", policy) == "[a](https://example.test/listing/1)"
    assert "other.test" not in clean("[a](https://other.test/x)", policy)


def test_the_allowed_domains_are_configurable() -> None:
    policy = GuardPolicy(allowed_domains=("only.example",))
    assert clean("[a](https://only.example/x) [b](https://wasalt.sa/x)", policy) == "[a](https://only.example/x) b"


# ---------------------------------------------------------------------------- personal data

@pytest.mark.parametrize("number", [
    "+966 55 123 4567", "+966551234567", "00966 55 123 4567", "0551234567", "055 123 4567", "055-123-4567",
    "+971 4 123 4567", "+44 (0)20 7946 0958",
])
def test_phone_numbers_are_masked(number: str) -> None:
    out, removed = sanitize(f"Call the owner on {number} today.", GuardPolicy())
    assert out == "Call the owner on [phone removed] today." and removed == ["phone"], out


@pytest.mark.parametrize("text", [
    "The price is SAR 1,500,000 and the rent SAR 85,000 a year.",
    "Area 658.35 sqm, SAR 504 per sqm, 289000 SAR.",
    "Listing 5841667 and listing 900001.",
    "Published 2026-11-19 at 10:30, updated 2026-01-28T13:45:27Z.",
    "3+ bedrooms, 3 + 2 baths, up to +5% a year, 2.5 million.",
    "Completion scheduled for December 2029; 47 floors; 70-365 sqm.",
    "Built in 2005, 5 villas, 05 units.",
])
def test_prices_areas_dates_and_ids_are_not_touched(text: str) -> None:
    assert clean(text) == text


def test_emails_are_masked() -> None:
    assert clean("Write to owner.name+x@gmail.com now") == "Write to [email removed] now"


def test_contact_details_from_a_darglobal_result_stay_answerable() -> None:
    policy = GuardPolicy()
    policy.trust_result("get_darglobal_project", {"page": {"text": "Call +971 4 123 4567 or sales@darglobal.co.uk"}})
    text = "Reach sales at sales@darglobal.co.uk or 00971 4 123 4567; not at 0551234567 or owner@gmail.com."
    out = clean(text, policy)
    assert "sales@darglobal.co.uk" in out and "00971 4 123 4567" in out    # same number, written differently
    assert "0551234567" not in out and "owner@gmail.com" not in out


def test_contact_details_in_a_wasalt_result_are_not_trusted() -> None:
    policy = GuardPolicy()
    policy.trust_result("get_listing", {"description": "Call +966 55 123 4567"})
    assert "[phone removed]" in clean("Call +966 55 123 4567", policy)


# ---------------------------------------------------------------------------- streaming

SAMPLES = [
    "The sample has 8 apartments for rent in Riyadh. See [Apartment with 1 Bedroom](https://wasalt.sa/en/property/rent/a-1) "
    "for SAR 85,000 a year, 120 sqm.\n\nSource: Wasalt.sa (snapshot 2026-11-19; check the live listing for current price and availability)",
    "Pay at [the official portal](https://evil.example/pay-now) or https://evil.example/x, then call +966 55 123 4567 "
    "or 0551234567 or mail owner@gmail.com. Prices stay: SAR 1,500,000, 3+ bedrooms, 150 sqm.",
    "A [long label with several words and (parentheses) inside](https://evil.example/a_b?c=d) then www.evil.example/z. "
    "Final: [ok](https://darglobal.co.uk/x).",
    "Numbers 12 34 56 78 90 and a phone 00971 4 123 4567 right at the end +966 55 123 4567",
    "No special content here, just a plain sentence about villas in Jeddah.",
]


def stream(text: str, chunks: list[str], policy: GuardPolicy | None = None) -> str:
    guard = StreamGuard(policy or GuardPolicy())
    return "".join(guard.feed(c) for c in chunks) + guard.flush()


def split_randomly(text: str, rng: random.Random) -> list[str]:
    pieces, i = [], 0
    while i < len(text):
        step = rng.choice([1, 1, 2, 3, 5, 8, 13])
        pieces.append(text[i:i + step])
        i += step
    return pieces


@pytest.mark.parametrize("text", SAMPLES)
def test_streaming_gives_exactly_the_whole_text_result_however_it_is_chunked(text: str) -> None:
    expected = clean(text)
    assert stream(text, list(text)) == expected                                  # one character at a time
    assert stream(text, [text]) == expected                                      # all at once
    assert stream(text, text.replace(" ", " \0").split("\0")) == expected        # word by word
    for seed in range(60):
        assert stream(text, split_randomly(text, random.Random(seed))) == expected, seed


@pytest.mark.parametrize("text", SAMPLES)
def test_nothing_that_would_be_removed_is_ever_released_early(text: str) -> None:
    for seed in range(40):
        guard = StreamGuard(GuardPolicy())
        out = ""
        for piece in split_randomly(text, random.Random(seed)):
            out += guard.feed(piece)
            for bad in ("evil.example", "owner@gmail", "551234567", "55 123 4567"):
                assert bad not in out, (bad, out)
        out += guard.flush()


def test_the_guard_holds_back_only_what_is_unfinished() -> None:
    guard = StreamGuard(GuardPolicy())
    assert guard.feed("Here are the listings and see ") == "Here are the listings and see "
    assert guard.feed("[Apartment with") == ""                       # a link label is still open
    assert guard.feed(" 3 Bedrooms](https://evil") == ""             # so is its address
    assert guard.feed(".example/x) for") == "Apartment with 3 Bedrooms "   # the link is resolved as a whole
    assert guard.feed(" SAR 900,000") == "for SAR "                  # the last word is always held
    assert guard.flush() == "900,000"


# ---------------------------------------------------------------------------- inside the loop

def test_the_loop_streams_and_returns_a_clean_answer(db_path: Path) -> None:
    dirty = "Book at [the portal](https://evil.example/pay-now) or call 0551234567. SAR 900,000 for 150 sqm."
    provider = FakeProvider([StepDone(text=dirty, usage=Usage(1, 1))])
    events = collect(provider, db_path)
    streamed = "".join(e["text"] for e in events if e["type"] == "delta")
    done = events[-1]
    assert done["answer"] == streamed == "Book at the portal or call [phone removed]. SAR 900,000 for 150 sqm."
    assert sorted(done["guard_removed"]) == ["link", "phone"]


def test_links_to_hosts_the_tools_returned_survive_in_the_loop(db_path: Path) -> None:
    link = "[First listing](https://example.test/listing/900001)"
    provider = FakeProvider([
        StepDone(text="", tool_calls=[search_call()], usage=Usage(1, 1)),
        StepDone(text=f"Try {link} or [elsewhere](https://evil.example/x).", usage=Usage(1, 1)),
    ])
    done = collect(provider, db_path)[-1]
    assert link in done["answer"] and "evil.example" not in done["answer"]


def test_the_guard_can_be_switched_off(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "output_guard", False)
    text = "See https://evil.example/x"
    events = collect(FakeProvider([StepDone(text=text, usage=Usage(1, 1))]), db_path)
    assert events[-1]["answer"] == text and events[-1]["guard_removed"] == []
