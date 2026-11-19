"""HTTP API, with the Chainlit chat UI mounted at the site root."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from estatequery import observability
from estatequery.agent.loop import run_turn
from estatequery.agent.tools import connect
from estatequery.api.limits import get_limiter
from estatequery.config import MODELS, get_model, get_settings
from estatequery.ui.identity import COOKIE_MAX_AGE, VISITOR_COOKIE, new_visitor_id, visitor_from_cookie_header

log = logging.getLogger("estatequery.api")
CHAINLIT_APP = Path(__file__).resolve().parent.parent / "ui" / "chainlit_app.py"
HEARTBEAT_SECONDS = 15
TURN_TIMEOUT_SECONDS = 120


class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=8000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    model: str | None = None
    session_id: str | None = Field(default=None, max_length=64)
    history: list[HistoryMessage] = Field(default_factory=list, max_length=40)


class FeedbackRequest(BaseModel):
    trace_id: str = Field(min_length=8, max_length=64)
    value: Literal[0, 1]
    comment: str | None = Field(default=None, max_length=500)


def dataset_info() -> dict[str, Any]:
    try:
        conn = connect()
    except sqlite3.Error:
        return {"ready": False}
    try:
        meta = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM meta")}
        return {
            "ready": True,
            "snapshot_date": meta.get("snapshot_date"),
            "listings": int(meta.get("listings", 0)),
            "darglobal_pages": int(meta.get("pages", 0)),
            "darglobal_projects": int(meta.get("projects", 0)),
        }
    except sqlite3.Error:
        return {"ready": False}
    finally:
        conn.close()


def client_ip(request: Request) -> str:
    # Caddy is the only thing in front of the app and sets X-Forwarded-For.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    info = dataset_info()
    if info.get("ready"):
        log.info("dataset ready: %s", info)
    else:
        log.warning("database not found at %s; run `estatequery build-db`", get_settings().db_path)
    yield
    observability.flush()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="EstateQuery", docs_url=None, redoc_url=None, lifespan=lifespan)
    limiter = get_limiter()

    @app.middleware("http")
    async def visitor_cookie(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Give each browser an anonymous id so the chat UI can keep its history apart."""
        response = await call_next(request)
        if (request.method == "GET" and request.url.path != "/healthz"
                and visitor_from_cookie_header(request.headers.get("cookie")) is None):
            https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
            response.set_cookie(VISITOR_COOKIE, new_visitor_id(), max_age=COOKIE_MAX_AGE,
                                httponly=True, samesite="lax", secure=https, path="/")
        return response

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        info = dataset_info()
        return JSONResponse({"status": "ok" if info.get("ready") else "no-data", **info},
                            status_code=200 if info.get("ready") else 503)

    @app.get("/api/models")
    async def models() -> dict[str, Any]:
        available = [
            {"id": m.id, "label": m.label, "provider": m.provider, "note": m.note}
            for m in MODELS
            if (m.provider == "openrouter" and settings.openrouter_api_key)
            or (m.provider == "anthropic" and settings.anthropic_api_key)
        ]
        return {"models": available, "default": get_model(None).id, "dataset": dataset_info()}

    @app.post("/api/chat")
    async def chat(body: ChatRequest, request: Request) -> StreamingResponse:
        spec = get_model(body.model)
        message = body.message.strip()

        async def refuse(text: str) -> AsyncIterator[str]:
            yield sse({"type": "error", "message": text, "retryable": False})

        if len(message) > settings.max_message_chars:
            return StreamingResponse(
                refuse(f"Message is too long (limit {settings.max_message_chars} characters)."),
                media_type="text/event-stream")
        decision = limiter.check(client_ip(request), spec.provider)
        if not decision.allowed:
            return StreamingResponse(refuse(decision.reason), media_type="text/event-stream",
                                     headers={"Retry-After": str(decision.retry_after)})

        async def stream() -> AsyncIterator[str]:
            queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

            async def produce() -> None:
                try:
                    async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
                        async for event in run_turn(
                            spec=spec,
                            history=[m.model_dump() for m in body.history],
                            user_message=message,
                            session_id=body.session_id,
                        ):
                            await queue.put(event)
                except TimeoutError:
                    await queue.put({"type": "error", "retryable": True,
                                     "message": "The model took too long to answer. Please try again."})
                finally:
                    await queue.put(None)

            task = asyncio.create_task(produce())
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                    except TimeoutError:
                        yield ": keep-alive\n\n"  # comment frame keeps proxies from closing the stream
                        continue
                    if event is None:
                        break
                    yield sse(event)
            finally:
                task.cancel()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/feedback")
    async def feedback(body: FeedbackRequest) -> dict[str, bool]:
        ok = observability.score(body.trace_id, "user-feedback", float(body.value), body.comment)
        return {"recorded": ok}

    # The chat UI. Mounted last so the API routes above take precedence.
    from chainlit.utils import mount_chainlit

    mount_chainlit(app=app, target=str(CHAINLIT_APP), path="/")
    return app


app = create_app()
