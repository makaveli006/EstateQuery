"""Which model each visitor has picked.

The picker in the message box posts the choice here, and the chat handler reads it for
every message. Keeping it on the server, keyed by the anonymous visitor cookie, means
switching model needs no Chainlit profile or setting, so it never restarts the chat.

The store is in memory: the browser also remembers the choice and sends it again on page
load, so a server restart loses nothing a visitor would notice.
"""

from __future__ import annotations

from collections import OrderedDict

from estatequery.config import ModelSpec, available_models, get_model


class ModelChoices:
    """A small bounded map of visitor id -> model id; the least recently used entry goes first."""

    def __init__(self, capacity: int = 20_000) -> None:
        self._capacity = capacity
        self._choices: OrderedDict[str, str] = OrderedDict()

    def set(self, visitor_id: str, model_id: str) -> None:
        self._choices[visitor_id] = model_id
        self._choices.move_to_end(visitor_id)
        while len(self._choices) > self._capacity:
            self._choices.popitem(last=False)

    def get(self, visitor_id: str | None) -> str | None:
        if not visitor_id or visitor_id not in self._choices:
            return None
        self._choices.move_to_end(visitor_id)
        return self._choices[visitor_id]

    def __len__(self) -> int:
        return len(self._choices)


choices = ModelChoices()


def find_available(model_id: str | None) -> ModelSpec | None:
    """The model with this id, if it exists and its provider is configured."""
    return next((m for m in available_models() if m.id == model_id), None)


def default_model() -> ModelSpec:
    models = available_models()
    preferred = get_model(None)
    return preferred if preferred in models or not models else models[0]


def model_for(visitor_id: str | None) -> ModelSpec:
    """The visitor's chosen model, or the default if they have not chosen (or it went away)."""
    return find_available(choices.get(visitor_id)) or default_model()
