# syntax=docker/dockerfile:1

# ---- build: resolve and install locked dependencies with uv ------------------
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.21 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependencies first, so this layer is cached until the lock file changes.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# ---- runtime: slim image, non-root, data baked in -----------------------------
FROM python:3.13-slim
RUN useradd --create-home --uid 10001 app
WORKDIR /app

COPY --from=build /app/.venv /app/.venv

# The read-only SQLite database built by `estatequery build-db`. It is not in git
# (the Wasalt policy forbids redistribution), so it must exist locally at build time.
ARG DB_PATH=data/estatequery.db
COPY ${DB_PATH} /app/data/estatequery.db

# Chainlit reads its config, welcome text and static assets from the working directory.
COPY .chainlit/config.toml /app/.chainlit/config.toml
COPY .chainlit/translations/en-US.json /app/.chainlit/translations/en-US.json
COPY chainlit.md /app/chainlit.md
COPY public /app/public
RUN mkdir -p /app/.files && chown -R app:app /app/.chainlit /app/.files

ENV PATH="/app/.venv/bin:$PATH" \
    DATA_DIR=/app/data \
    PYTHONUNBUFFERED=1

USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"

# One worker on purpose: rate limits and budget caps live in process memory.
CMD ["estatequery", "serve", "--host", "0.0.0.0", "--port", "8000"]
