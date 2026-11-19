"""Abuse protection for a public demo backed by paid API keys.

In-memory and per-process, which is correct for the single-worker deployment this app
uses. Three independent limits: requests per minute per IP, requests per day per IP,
and a global daily cap per provider so a burst from many addresses cannot drain a key.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date
from functools import lru_cache


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    retry_after: int = 0


class Limiter:
    def __init__(self, per_minute: int, per_day: int, provider_caps: dict[str, int]) -> None:
        self.per_minute = per_minute
        self.per_day = per_day
        self.provider_caps = provider_caps
        self._minute: dict[str, deque[float]] = defaultdict(deque)
        self._day: dict[str, int] = defaultdict(int)
        self._provider: dict[str, int] = defaultdict(int)
        self._today = date.today()

    def _roll_day(self) -> None:
        today = date.today()
        if today != self._today:
            self._today = today
            self._day.clear()
            self._provider.clear()

    def check(self, ip: str, provider: str, now: float | None = None) -> Decision:
        """Count one request if it is allowed; otherwise say why not."""
        now = time.monotonic() if now is None else now
        self._roll_day()

        window = self._minute[ip]
        while window and now - window[0] >= 60:
            window.popleft()
        if len(window) >= self.per_minute:
            return Decision(False, "Too many messages in a short time. Please wait a moment.",
                            retry_after=max(1, int(60 - (now - window[0]))))
        if self._day[ip] >= self.per_day:
            return Decision(False, "Daily message limit reached for this demo. Please come back tomorrow.",
                            retry_after=3600)
        cap = self.provider_caps.get(provider)
        if cap is not None and self._provider[provider] >= cap:
            return Decision(False, "This model provider has reached today's demo budget. Try another model.",
                            retry_after=3600)

        window.append(now)
        self._day[ip] += 1
        self._provider[provider] += 1
        if len(self._minute) > 5000:  # keep memory bounded under address churn
            for key in [k for k, v in self._minute.items() if not v]:
                del self._minute[key]
        return Decision(True)


@lru_cache
def get_limiter() -> Limiter:
    """One limiter per process, shared by the JSON API and the chat UI."""
    from estatequery.config import get_settings

    settings = get_settings()
    return Limiter(
        settings.rate_limit_per_minute,
        settings.rate_limit_per_day,
        {"openrouter": settings.daily_cap_openrouter, "anthropic": settings.daily_cap_anthropic},
    )
