"""Chainlit chat UI.

A thin front end over the same agent loop the JSON API uses: it renders the loop's
events (streamed text, tool calls, sources) and adds nothing to the agent itself.
The model is chosen with a chat profile, so the same question can be put to an
OpenRouter model and to Claude side by side.
"""

from __future__ import annotations

import json
import os
from typing import Any

import chainlit as cl

from estatequery import observability
from estatequery.agent.loop import run_turn
from estatequery.api.limits import get_limiter
from estatequery.config import MODELS, ModelSpec, get_model, get_settings
from estatequery.ui.identity import guest_identifier, new_visitor_id, visitor_from_cookie_header

TOOL_LABELS = {
    "search_listings": "Search Wasalt listings",
    "listing_stats": "Compute price statistics",
    "get_listing": "Open listing",
    "list_darglobal_projects": "List DarGlobal projects",
    "search_darglobal": "Search DarGlobal pages",
    "get_darglobal_project": "Open DarGlobal project",
}
STARTERS = [
    ("DarGlobal in Oman", "Which projects does DarGlobal have in Oman?"),
    ("Brand partners", "Which brands does DarGlobal partner with, and on which projects?"),
    ("Trump Tower Jeddah", "Tell me about Trump Tower Jeddah: unit types, amenities and completion date."),
    ("Listings in Riyadh", "Show me apartments for sale or rent in Riyadh."),
]
MAX_SOURCES_SHOWN = 6
FOOTER_SEPARATOR = "\n\n---\n"  # divides the model's answer from the sources/metadata footer

# Chat history (the sidebar) needs somewhere to store conversations. It switches on when
# a Postgres URL is configured and stays off otherwise, so local runs need no database.
DATABASE_URL = os.environ.get("DATABASE_URL", "")
HISTORY_ENABLED = bool(DATABASE_URL)


def available_models() -> list[ModelSpec]:
    settings = get_settings()
    return [
        m for m in MODELS
        if (m.provider == "openrouter" and settings.openrouter_api_key)
        or (m.provider == "anthropic" and settings.anthropic_api_key)
    ]


def spec_for_profile(profile_name: str | None) -> ModelSpec:
    for model in available_models():
        if model.label == profile_name:
            return model
    return get_model(None)


def client_ip() -> str:
    """Caller address for rate limiting: the proxy header if present, else the session."""
    try:
        environ = cl.context.session.environ or {}
        forwarded = environ.get("HTTP_X_FORWARDED_FOR") or environ.get("REMOTE_ADDR")
        if forwarded:
            return str(forwarded).split(",")[0].strip()
    except Exception:  # noqa: BLE001 - no request context (tests, background tasks)
        pass
    return str(cl.user_session.get("id") or "unknown")


@cl.set_chat_profiles
async def chat_profiles(*_: Any, **__: Any) -> list[cl.ChatProfile]:
    default_id = get_model(None).id
    return [
        cl.ChatProfile(
            name=m.label,
            markdown_description=f"**{m.label}** through the {m.note}.",
            default=(m.id == default_id),
        )
        for m in available_models()
    ]


@cl.set_starters
async def starters(*_: Any, **__: Any) -> list[cl.Starter]:
    return [cl.Starter(label=label, message=message) for label, message in STARTERS]


@cl.on_chat_start
async def on_chat_start() -> None:
    cl.user_session.set("history", [])


@cl.on_message
async def on_message(message: cl.Message) -> None:
    settings = get_settings()
    spec = spec_for_profile(cl.user_session.get("chat_profile"))
    text = (message.content or "").strip()
    if not text:
        return
    if len(text) > settings.max_message_chars:
        await cl.Message(content=f"That message is too long (limit {settings.max_message_chars} characters).").send()
        return
    decision = get_limiter().check(client_ip(), spec.provider)
    if not decision.allowed:
        await cl.Message(content=decision.reason).send()
        return

    history: list[dict[str, str]] = cl.user_session.get("history") or []
    answer: cl.Message | None = None
    open_steps: dict[str, list[cl.Step]] = {}
    sources: list[dict[str, str]] = []

    async for event in run_turn(spec=spec, history=history, user_message=text,
                                session_id=cl.user_session.get("id"), tags=["chainlit"]):
        kind = event["type"]
        if kind == "delta":
            if answer is None:
                answer = cl.Message(content="")
            await answer.stream_token(event["text"])
        elif kind == "tool_start":
            if answer is not None:
                # Text before a tool call is a preamble, not the answer; drop it so the
                # answer appears once, after the tool steps.
                await answer.remove()
                answer = None
            step = cl.Step(name=TOOL_LABELS.get(event["tool"], event["tool"]), type="tool")
            step.input = event["args"]
            await step.send()
            open_steps.setdefault(event["tool"], []).append(step)
        elif kind == "tool_end":
            pending = open_steps.get(event["tool"]) or []
            if pending:
                step = pending.pop(0)
                step.output = event["summary"]
                await step.update()
        elif kind == "sources":
            sources = event["items"]
        elif kind == "done":
            if answer is None:
                answer = cl.Message(content=event["answer"])
            footer = []
            if sources:
                links = " · ".join(f"[{s['title']}]({s['url']})" for s in sources[:MAX_SOURCES_SHOWN])
                more = f" · +{len(sources) - MAX_SOURCES_SHOWN} more" if len(sources) > MAX_SOURCES_SHOWN else ""
                footer.append(f"**Sources:** {links}{more}")
            rounds = event["tool_rounds"]
            footer.append(
                f"*{spec.label} · {event['latency_ms'] / 1000:.1f} s · "
                f"{event['input_tokens'] + event['output_tokens']:,} tokens · "
                f"{rounds} tool round{'' if rounds == 1 else 's'}*"
            )
            answer.content = event["answer"] + FOOTER_SEPARATOR + "\n\n".join(footer)
            # Stored with the message, so feedback given later can be tied to its trace.
            answer.metadata = {"trace_id": event.get("trace_id"), "model": spec.id}
            if event.get("trace_id") and not HISTORY_ENABLED:
                # With history on, Chainlit shows its own thumbs up/down on every answer.
                answer.actions = [
                    cl.Action(name="feedback", label="👍 Helpful", payload={"trace_id": event["trace_id"], "value": 1}),
                    cl.Action(name="feedback", label="👎 Not helpful", payload={"trace_id": event["trace_id"], "value": 0}),
                ]
            await answer.send()  # ends the token stream and attaches the feedback actions
            history += [{"role": "user", "content": text}, {"role": "assistant", "content": event["answer"]}]
            cl.user_session.set("history", history[-settings.max_history_messages:])
        elif kind == "error":
            if answer is not None:
                await answer.remove()
            await cl.Message(content=f"⚠️ {event['message']}").send()


@cl.action_callback("feedback")
async def on_feedback(action: cl.Action) -> None:
    payload = action.payload or {}
    observability.score(str(payload.get("trace_id") or ""), "user-feedback", float(payload.get("value", 0)))
    await action.remove()


# --------------------------------------------------------------------------- history

if HISTORY_ENABLED:
    from chainlit.data.chainlit_data_layer import ChainlitDataLayer
    from chainlit.types import Feedback, ThreadDict

    class HistoryDataLayer(ChainlitDataLayer):
        """Chainlit's Postgres store, plus forwarding of thumbs up/down to Langfuse."""

        async def upsert_feedback(self, feedback: Feedback) -> str:
            feedback_id = await super().upsert_feedback(feedback)
            try:
                rows = await self.execute_query('SELECT metadata FROM "Step" WHERE id = $1', {"id": feedback.forId})
                raw = rows[0]["metadata"] if rows else {}
                metadata = json.loads(raw) if isinstance(raw, str) else (raw or {})
                if metadata.get("trace_id"):
                    observability.score(str(metadata["trace_id"]), "user-feedback", float(feedback.value),
                                        feedback.comment)
            except Exception:  # noqa: BLE001 - the feedback itself is already saved
                pass
            return feedback_id

    @cl.data_layer
    def data_layer() -> HistoryDataLayer:
        return HistoryDataLayer(database_url=DATABASE_URL)

    @cl.header_auth_callback
    async def sign_in_guest(headers: Any) -> cl.User:
        """Sign every visitor in as an anonymous guest keyed by their browser cookie."""
        visitor = visitor_from_cookie_header(headers.get("cookie")) or new_visitor_id()
        return cl.User(identifier=guest_identifier(visitor), metadata={"role": "guest"})

    @cl.on_chat_resume
    async def on_chat_resume(thread: ThreadDict) -> None:
        """Rebuild the agent's memory when a past conversation is reopened."""
        history: list[dict[str, str]] = []
        for step in thread.get("steps") or []:
            text = (step.get("output") or "").strip()
            if not text:
                continue
            if step.get("type") == "user_message":
                history.append({"role": "user", "content": text})
            elif step.get("type") == "assistant_message" and FOOTER_SEPARATOR in text:
                # Only real answers carry the footer; notices and errors are skipped.
                history.append({"role": "assistant", "content": text.split(FOOTER_SEPARATOR)[0]})
        cl.user_session.set("history", history[-get_settings().max_history_messages:])
