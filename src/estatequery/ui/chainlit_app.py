"""Chainlit chat UI.

A thin front end over the same agent loop the JSON API uses: it renders the loop's
events (streamed text, tool calls, sources) and adds nothing to the agent itself.
The model is chosen with the picker in the message box (public/model-picker.js) and can
be changed mid-conversation, so the same question can be put to an OpenRouter model and
to Claude side by side.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import chainlit as cl
from chainlit.data import get_data_layer
from chainlit.utils import utc_now

from estatequery import observability
from estatequery.agent.loop import run_turn
from estatequery.api.limits import get_limiter
from estatequery.config import MODELS_BY_ID, get_settings
from estatequery.ui.activity import Activity
from estatequery.ui.identity import guest_identifier, new_visitor_id, visitor_from_cookie_header
from estatequery.ui.model_choice import model_for

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
FOOTER_SEPARATOR = "\n\n---\n"  # divides the model's answer from the sources/metadata footer

# Chat history (the sidebar) needs somewhere to store conversations. It switches on when
# a Postgres URL is configured and stays off otherwise, so local runs need no database.
DATABASE_URL = os.environ.get("DATABASE_URL", "")
HISTORY_ENABLED = bool(DATABASE_URL)


def visitor_id() -> str | None:
    """The anonymous visitor behind this chat session, from the cookie sent when it connected."""
    try:
        environ = cl.context.session.environ or {}
        return visitor_from_cookie_header(environ.get("HTTP_COOKIE"))
    except Exception:  # noqa: BLE001 - no request context (tests, background tasks)
        return None


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


@cl.set_starters
async def starters(*_: Any, **__: Any) -> list[cl.Starter]:
    return [cl.Starter(label=label, message=message) for label, message in STARTERS]


@cl.on_chat_start
async def on_chat_start() -> None:
    cl.user_session.set("history", [])


@cl.on_message
async def on_message(message: cl.Message) -> None:
    settings = get_settings()
    spec = model_for(visitor_id())
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
    sources: list[dict[str, str]] = []
    research = ResearchSection()

    try:
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
                    # answer appears once, after the research section.
                    await answer.remove()
                    answer = None
                await research.start(event["call_id"], TOOL_LABELS.get(event["tool"], event["tool"]), event["args"])
            elif kind == "tool_end":
                await research.finish(event["call_id"], event["summary"], event.get("ok", True))
            elif kind == "sources":
                sources = event["items"]
            elif kind == "done":
                await research.close()
                if answer is None:
                    answer = cl.Message(content=event["answer"])
                footer = []
                if sources:
                    footer.append("**Sources:** " + " · ".join(f"[{s['title']}]({s['url']})" for s in sources))
                rounds = event["tool_rounds"]
                footer.append(
                    f"*{MODELS_BY_ID.get(event.get('model'), spec).label} · {event['latency_ms'] / 1000:.1f} s · "
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
                await research.close()
                if answer is not None:
                    await answer.remove()
                await cl.Message(content=f"⚠️ {event['message']}").send()
    finally:
        # Reached on a normal finish (a no-op then) and when the turn is cut short, for
        # example by the stop button: no row may be left looking as if it is still running.
        await research.close()
        await research.persist_final()


class ResearchSection:
    """One collapsible "Research activity" section for a turn, with a row per tool call.

    Every step is given its parent explicitly. Chainlit only does that for steps used as
    context managers; a step that is just sent has no parent, sits outside the turn's run,
    and is drawn after the answer that belongs to the run. Parenting the section to the run,
    and each row to the section, is what puts the activity before the answer, both live and
    when the conversation is reloaded from storage.
    """

    def __init__(self) -> None:
        self.activity = Activity()
        self.group: cl.Step | None = None
        self.rows: dict[str, cl.Step] = {}
        self.closed = False
        self.persisted = False

    async def start(self, call_id: str, label: str, args: dict[str, Any]) -> None:
        call = self.activity.start(call_id, label)
        if call is None:
            return  # this execution already has a row
        if self.group is None:
            run = cl.context.current_run
            self.group = cl.Step(name=self.activity.title, type="tool", parent_id=run.id if run else None,
                                 default_open=True, auto_collapse=True, show_input=False)
            self.group.start = utc_now()
            await self.group.send()
        else:
            self.group.name = self.activity.title
            await self.group.update()
        row = cl.Step(name=call.title, type="tool", parent_id=self.group.id, show_input="json")
        row.start = utc_now()
        row.input = args
        self.rows[call_id] = row
        await row.send()

    async def finish(self, call_id: str, summary: str, ok: bool) -> None:
        call = self.activity.finish(call_id, ok)
        row = self.rows.get(call_id)
        if call is None or row is None:
            return
        await self._settle(row, call.title, summary, failed=not ok)
        if self.group is not None:
            self.group.name = self.activity.title
            await self.group.update()

    async def close(self) -> None:
        """Finish the section: stop any row still running, set the final title, collapse."""
        if self.closed or self.group is None:
            self.closed = True
            return
        self.closed = True
        try:
            for call_id in self.activity.interrupt_running():
                await self._settle(self.rows[call_id], self.activity.calls[call_id].title, "interrupted", failed=True)
            self.group.name = self.activity.title
            self.group.output = self.activity.summary
            self.group.end = utc_now()
            await self.group.update()
        except BaseException:  # noqa: BLE001 - closing must never mask the original outcome
            pass

    async def persist_final(self) -> None:
        """Store the final state of every row and of the section, in order.

        Chainlit saves a step when it is sent and again when it is updated, each time as a
        background task it does not wait for. A tool that finishes within milliseconds has
        both writes in flight at once, and the earlier one can land last, leaving "running"
        in storage for a call that succeeded. Writing the final state once more, awaited and
        after those tasks have had time to finish, makes a reloaded conversation show the
        same rows the live one did.
        """
        if self.persisted or self.group is None:
            return
        self.persisted = True
        data_layer = get_data_layer()
        if data_layer is None:
            return
        try:
            await asyncio.sleep(0.3)
            for step in (*self.rows.values(), self.group):
                await data_layer.update_step(step.to_dict())
        except BaseException:  # noqa: BLE001 - best effort; the live view is already correct
            pass

    @staticmethod
    async def _settle(row: cl.Step, title: str, output: str, *, failed: bool) -> None:
        row.name = title
        row.output = output
        row.is_error = failed
        row.end = utc_now()
        await row.update()


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
