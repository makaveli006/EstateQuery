"""Failover support: a small circuit breaker and the list of models to fall back to.

A provider outage should cost one slow request, not every request. After a few retryable
failures of the same model within a minute the circuit opens: for a short cooldown the agent
loop goes straight to the next model instead of waiting out the timeout again. When the cooldown
ends the next request is a trial: if it succeeds the circuit closes, if it fails it reopens at once.
"""

from __future__ import annotations

import time
from typing import Callable

from estatequery.config import MODELS_BY_ID, ModelSpec, available_models


class CircuitBreaker:
    def __init__(self, threshold: int = 3, window: float = 60.0, cooldown: float = 60.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.threshold, self.window, self.cooldown, self.clock = threshold, window, cooldown, clock
        self._failures: dict[str, list[float]] = {}
        self._open_until: dict[str, float] = {}
        self._trial: set[str] = set()          # circuits whose cooldown ended and have not yet been tried

    def is_open(self, key: str) -> bool:
        until = self._open_until.get(key)
        if until is None:
            return False
        if self.clock() < until:
            return True
        del self._open_until[key]              # cooldown over: let one request try again
        self._trial.add(key)
        return False

    def record_failure(self, key: str) -> None:
        now = self.clock()
        if key in self._trial:                 # the trial request failed: reopen immediately
            self._trial.discard(key)
            self._open_until[key] = now + self.cooldown
            return
        recent = [t for t in self._failures.get(key, []) if now - t < self.window] + [now]
        if len(recent) >= self.threshold:
            self._open_until[key] = now + self.cooldown
            recent = []
        self._failures[key] = recent

    def record_success(self, key: str) -> None:
        self._failures.pop(key, None)
        self._open_until.pop(key, None)
        self._trial.discard(key)


BREAKER = CircuitBreaker()


def fallback_specs(spec: ModelSpec, configured: str) -> list[ModelSpec]:
    """The models to try after `spec`, from a comma-separated list of model ids.

    Unknown ids, the model itself, and models whose provider has no API key are left out. An id
    from another provider gives cross-provider failover with no code change.
    """
    usable = {m.id for m in available_models()}
    chosen: list[ModelSpec] = []
    for model_id in (part.strip() for part in configured.split(",")):
        model = MODELS_BY_ID.get(model_id)
        if model and model.id != spec.id and model.id in usable and model not in chosen:
            chosen.append(model)
    return chosen
