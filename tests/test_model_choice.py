from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from estatequery.config import MODELS
from estatequery.ui import model_choice
from estatequery.ui.identity import VISITOR_COOKIE, new_visitor_id
from estatequery.ui.model_choice import ModelChoices

SONNET = "anthropic:claude-sonnet-5-5"
HAIKU = "anthropic:claude-haiku-5-5"


@pytest.fixture(autouse=True)
def all_models_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every registered model selectable, whatever API keys the machine has."""
    monkeypatch.setattr(model_choice, "available_models", lambda: list(MODELS))
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


def test_model_for_falls_back_to_the_default() -> None:
    visitor = new_visitor_id()
    default = model_choice.default_model()
    assert model_choice.model_for(visitor) == default
    assert model_choice.model_for(None) == default

    model_choice.choices.set(visitor, SONNET)
    assert model_choice.model_for(visitor).id == SONNET

    # A stored choice that no longer exists must not break the chat.
    model_choice.choices.set(visitor, "openrouter:removed/model")
    assert model_choice.model_for(visitor) == default


def test_api_sets_and_returns_the_visitors_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    from estatequery.api import app as app_module

    monkeypatch.setattr(app_module, "choices", model_choice.choices)
    client = TestClient(app_module.app)
    default_id = model_choice.default_model().id

    # No cookie yet: reading works (default), writing is refused.
    assert client.get("/api/model").json() == {"model": default_id}
    cookieless = TestClient(app_module.app)
    assert cookieless.post("/api/model", json={"model": SONNET}).status_code == 409

    # The first GET issued the visitor cookie; from now on the choice sticks.
    assert VISITOR_COOKIE in client.cookies
    assert client.post("/api/model", json={"model": SONNET}).json() == {"model": SONNET}
    assert client.get("/api/model").json() == {"model": SONNET}

    assert client.post("/api/model", json={"model": "openrouter:not/a-model"}).status_code == 400
    assert client.get("/api/model").json() == {"model": SONNET}

    # Another browser is unaffected.
    other = TestClient(app_module.app)
    assert other.get("/api/model").json() == {"model": default_id}
