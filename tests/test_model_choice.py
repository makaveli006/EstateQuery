from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from estatequery import config
from estatequery.config import MODELS, MODELS_BY_ID, get_model, get_settings
from estatequery.ui import model_choice
from estatequery.ui.identity import VISITOR_COOKIE, new_visitor_id
from estatequery.ui.model_choice import ModelChoices

SONNET = "anthropic:claude-sonnet-5-5"
HAIKU = "anthropic:claude-haiku-5-5"
OPUS = "anthropic:claude-opus-5-5"
DEEPSEEK = "openrouter:deepseek/deepseek-v4-flash"


def every_provider_configured() -> list:
    """What listed_models() returns when both API keys are set, whatever this machine has."""
    return [m for m in MODELS if m.listed]


@pytest.fixture(autouse=True)
def both_providers_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "available_models", lambda: list(MODELS))
    monkeypatch.setattr(model_choice, "choices", ModelChoices())


def test_store_remembers_per_visitor_and_evicts_the_least_recently_used() -> None:
    store = ModelChoices(capacity=2)
    store.set("a", SONNET)
    store.set("b", HAIKU)
    assert store.get("a") == SONNET  # reading "a" makes "b" the oldest
    store.set("c", HAIKU)
    assert store.get("b") is None
    assert store.get("a") == SONNET and store.get("c") == HAIKU
    assert len(store) == 2
    assert store.get(None) is None and store.get("unknown") is None


def test_only_sonnet_and_haiku_are_listed_with_sonnet_as_default() -> None:
    assert [m.id for m in config.listed_models()] == [SONNET, HAIKU]
    assert get_settings().default_model == SONNET
    assert model_choice.default_model().id == SONNET


def test_hidden_models_stay_in_the_registry_and_remain_usable_by_code() -> None:
    for hidden in (OPUS, DEEPSEEK, "openrouter:google/gemini-3.8-flash",
                   "openrouter:google/gemma-4-31b-it:free", "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free"):
        assert hidden in MODELS_BY_ID and not MODELS_BY_ID[hidden].listed
        assert get_model(hidden).id == hidden          # the JSON API and evaluations can still name it
    assert {m.provider for m in MODELS} == {"anthropic", "openrouter"}  # the OpenRouter adapter's models remain


def test_model_for_falls_back_to_the_default() -> None:
    visitor = new_visitor_id()
    assert model_choice.model_for(visitor).id == SONNET
    assert model_choice.model_for(None).id == SONNET

    model_choice.choices.set(visitor, HAIKU)
    assert model_choice.model_for(visitor).id == HAIKU

    # A choice made before a model was hidden (or removed) must not keep the visitor on it.
    model_choice.choices.set(visitor, DEEPSEEK)
    assert model_choice.model_for(visitor).id == SONNET
    model_choice.choices.set(visitor, "openrouter:removed/model")
    assert model_choice.model_for(visitor).id == SONNET


def test_api_lists_only_visible_models_and_sets_the_visitors_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    from estatequery.api import app as app_module

    monkeypatch.setattr(app_module, "choices", model_choice.choices)
    client = TestClient(app_module.app)

    listing = client.get("/api/models").json()
    assert [m["id"] for m in listing["models"]] == [SONNET, HAIKU]
    assert listing["default"] == SONNET
    assert all(m["tier"] != "more" for m in listing["models"])  # so the picker shows no "More models" row

    # No cookie yet: reading works (default), writing is refused.
    assert client.get("/api/model").json() == {"model": SONNET}
    assert TestClient(app_module.app).post("/api/model", json={"model": HAIKU}).status_code == 409

    # The first GET issued the visitor cookie; from now on the choice sticks.
    assert VISITOR_COOKIE in client.cookies
    assert client.post("/api/model", json={"model": HAIKU}).json() == {"model": HAIKU}
    assert client.get("/api/model").json() == {"model": HAIKU}

    # Hidden and unknown models are refused, and the earlier choice is untouched.
    for refused in (OPUS, DEEPSEEK, "openrouter:not/a-model"):
        assert client.post("/api/model", json={"model": refused}).status_code == 400
    assert client.get("/api/model").json() == {"model": HAIKU}

    # Another browser is unaffected.
    assert TestClient(app_module.app).get("/api/model").json() == {"model": SONNET}


def test_a_visitor_stuck_on_a_hidden_model_is_moved_to_the_default_consistently(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The chip (GET /api/model) and the model that answers (model_for) must agree."""
    from estatequery.api import app as app_module

    monkeypatch.setattr(app_module, "choices", model_choice.choices)
    client = TestClient(app_module.app)
    client.get("/api/model")                                  # obtain the visitor cookie
    visitor = client.cookies[VISITOR_COOKIE]
    model_choice.choices.set(visitor, DEEPSEEK)               # chosen back when it was still listed
    assert client.get("/api/model").json() == {"model": SONNET}
    assert model_choice.model_for(visitor).id == SONNET
