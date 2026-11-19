"""Langfuse tracing. Every call is best-effort: if Langfuse is down or unconfigured,
the chat keeps working and these helpers do nothing."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from estatequery.config import get_settings

log = logging.getLogger("estatequery.observability")


@lru_cache
def _langfuse() -> Any | None:
    settings = get_settings()
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return None
    try:
        from langfuse import Langfuse

        return Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            base_url=settings.langfuse_base_url,
        )
    except Exception:  # noqa: BLE001 - tracing must never break the app
        log.exception("Langfuse init failed; tracing disabled")
        return None


class _Observation:
    """Thin wrapper so callers never touch a Langfuse object directly."""

    def __init__(self, span: Any | None) -> None:
        self._span = span

    def end(self, **fields: Any) -> None:
        if self._span is None:
            return
        try:
            clean = {k: v for k, v in fields.items() if v is not None}
            if clean:
                self._span.update(**clean)
            self._span.end()
        except Exception:  # noqa: BLE001
            log.debug("Langfuse span end failed", exc_info=True)


class TurnTrace:
    """One trace per chat turn: a root agent span with generation and tool children."""

    def __init__(self, *, name: str, user_input: str, session_id: str | None, tags: list[str],
                 metadata: dict[str, Any]) -> None:
        self._client = _langfuse()
        self._attrs = {"session_id": session_id, "tags": tags, "trace_name": name,
                       "metadata": {k: str(v) for k, v in metadata.items()}}
        self._root: Any | None = None
        self.trace_id: str | None = None
        if self._client is None:
            return
        try:
            self._root = self._create(self._client, name=name, as_type="agent", input=user_input)
            self.trace_id = getattr(self._root, "trace_id", None)
        except Exception:  # noqa: BLE001
            log.debug("Langfuse trace start failed", exc_info=True)
            self._root = None

    def _create(self, parent: Any, **kwargs: Any) -> Any:
        from langfuse import propagate_attributes

        # Trace attributes are applied to spans created inside this context.
        with propagate_attributes(**self._attrs):
            return parent.start_observation(**kwargs)

    def _child(self, **kwargs: Any) -> _Observation:
        if self._root is None:
            return _Observation(None)
        try:
            return _Observation(self._create(self._root, **kwargs))
        except Exception:  # noqa: BLE001
            log.debug("Langfuse child span failed", exc_info=True)
            return _Observation(None)

    def generation(self, *, name: str, model: str, input: Any) -> _Observation:
        return self._child(name=name, as_type="generation", model=model, input=input)

    def tool(self, *, name: str, args: Any) -> _Observation:
        return self._child(name=name, as_type="tool", input=args)

    def finish(self, *, output: Any, error: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        if self._root is None:
            return
        try:
            fields: dict[str, Any] = {"output": output}
            if metadata:
                fields["metadata"] = metadata
            if error:
                fields.update(level="ERROR", status_message=error)
            self._root.update(**fields)
            self._root.end()
        except Exception:  # noqa: BLE001
            log.debug("Langfuse trace finish failed", exc_info=True)


def score(trace_id: str, name: str, value: float | str, comment: str | None = None) -> bool:
    client = _langfuse()
    if client is None or not trace_id:
        return False
    try:
        client.create_score(trace_id=trace_id, name=name, value=value, comment=comment)
        return True
    except Exception:  # noqa: BLE001
        log.debug("Langfuse score failed", exc_info=True)
        return False


def flush() -> None:
    client = _langfuse()
    if client is not None:
        try:
            client.flush()
        except Exception:  # noqa: BLE001
            log.debug("Langfuse flush failed", exc_info=True)
