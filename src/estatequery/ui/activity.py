"""Bookkeeping for the "Research activity" section of one chat turn.

The chat UI shows every tool call of a turn as one row inside a single collapsible
section. This module holds the state behind that section: which calls are running, which
succeeded or failed, and what the section and its rows should be titled. It knows nothing
about Chainlit, so it can be tested on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field

RUNNING, OK, FAILED, INTERRUPTED = "running", "ok", "failed", "interrupted"
SECTION_NAME = "Research activity"


@dataclass
class Call:
    label: str
    state: str = RUNNING

    @property
    def title(self) -> str:
        """The row's text. A running call shows its bare label; Chainlit animates it."""
        if self.state == OK:
            return f"✓ {self.label}"
        if self.state == FAILED:
            return f"✗ {self.label} (failed)"
        if self.state == INTERRUPTED:
            return f"✗ {self.label} (interrupted)"
        return self.label


@dataclass
class Activity:
    """Tool calls of one turn, in the order they started."""

    calls: dict[str, Call] = field(default_factory=dict)

    def start(self, call_id: str, label: str) -> Call | None:
        """Register a call. Returns None for an id already seen, so a repeated event
        cannot create a second row for the same execution."""
        if call_id in self.calls:
            return None
        self.calls[call_id] = Call(label)
        return self.calls[call_id]

    def finish(self, call_id: str, ok: bool) -> Call | None:
        """Record a call's outcome. Unknown or already finished ids are ignored."""
        call = self.calls.get(call_id)
        if call is None or call.state != RUNNING:
            return None
        call.state = OK if ok else FAILED
        return call

    def interrupt_running(self) -> list[str]:
        """Mark every call still running as interrupted; returns their ids."""
        stopped = [call_id for call_id, call in self.calls.items() if call.state == RUNNING]
        for call_id in stopped:
            self.calls[call_id].state = INTERRUPTED
        return stopped

    @property
    def total(self) -> int:
        return len(self.calls)

    def count(self, state: str) -> int:
        return sum(1 for call in self.calls.values() if call.state == state)

    @property
    def title(self) -> str:
        """Heading of the section, e.g. "Research activity · 7 tool executions"."""
        noun = "tool execution" if self.total == 1 else "tool executions"
        text = f"{SECTION_NAME} · {self.total} {noun}"
        problems = self.count(FAILED) + self.count(INTERRUPTED)
        if problems:
            text += f" · {problems} did not complete"
        return text

    @property
    def summary(self) -> str:
        """One line shown inside the section once the turn is over."""
        parts = [f"{self.count(OK)} completed"]
        if self.count(FAILED):
            parts.append(f"{self.count(FAILED)} failed")
        if self.count(INTERRUPTED):
            parts.append(f"{self.count(INTERRUPTED)} interrupted")
        return ", ".join(parts)
