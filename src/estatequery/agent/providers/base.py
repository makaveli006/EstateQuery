"""Provider-neutral types shared by the OpenRouter and Anthropic adapters.

The agent loop only knows these types. Each adapter keeps the conversation in its own
wire format inside a `Session`, so provider-specific blocks (Claude thinking blocks,
OpenAI tool_call ids) round-trip unchanged within a turn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol


class ProviderError(Exception):
    """A model call failed in a way worth showing to the user."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any] | None  # None when the model produced unparseable arguments
    raw_args: str = ""


@dataclass
class ToolResult:
    call_id: str
    name: str
    content: str  # JSON text
    is_error: bool = False


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens


@dataclass
class TextDelta:
    text: str


@dataclass
class StepDone:
    """End of one model call."""

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    stop_reason: str | None = None
    model: str | None = None  # the model that actually served the call


class Session(Protocol):
    def step(self, *, allow_tools: bool) -> AsyncIterator[TextDelta | StepDone]:
        """Run one model call, yielding text as it streams and a final StepDone."""
        ...

    def add_tool_results(self, results: list[ToolResult]) -> None:
        """Record the results for the tool calls of the last step."""
        ...


class Provider(Protocol):
    def start(
        self,
        *,
        model: str,
        system: str,
        history: list[dict[str, str]],
        user_message: str,
        tools: list[dict[str, Any]],
    ) -> Session: ...
