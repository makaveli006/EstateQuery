"""Dense retrieval: chunk embeddings stored in SQLite, and a cross-encoder reranker.

Both models run locally through `fastembed` (ONNX, no GPU, no API calls), which is an optional
dependency (`uv sync --extra retrieval`) and brings numpy with it. Embeddings are computed once, at
database build time, and stored in the `chunk_vectors` table; at query time only the question is
embedded. Everything here imports `fastembed` and numpy lazily and raises `RetrievalUnavailable` when
they or the vectors are missing, so callers can fall back to BM25 and the chat keeps working.
"""

from __future__ import annotations

import os
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from estatequery.config import get_settings

if TYPE_CHECKING:
    import numpy as np

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
TABLE = "chunk_vectors"
MAX_EMBED_CHARS = 1200
MAX_RERANK_CHARS = 600


class RetrievalUnavailable(RuntimeError):
    """Hybrid retrieval cannot run here (library, models or vectors missing)."""


def _numpy() -> Any:
    try:
        import numpy
    except ImportError as exc:
        raise RetrievalUnavailable("numpy is not installed (uv sync --extra retrieval)") from exc
    return numpy


def embedding_text(title: str | None, heading: str | None, text: str) -> str:
    """What is embedded for a chunk: its page title and heading give the passage its context."""
    return f"{title or ''}. {heading or ''}. {text}"[:MAX_EMBED_CHARS]


def rerank_text(title: str | None, text: str) -> str:
    return f"{title or ''}. {text}"[:MAX_RERANK_CHARS]


def _cache_dir() -> str:
    return os.environ.get("FASTEMBED_CACHE_DIR") or str(get_settings().model_cache_dir)


@lru_cache(maxsize=1)
def embedder() -> Any:
    try:
        from fastembed import TextEmbedding
    except ImportError as exc:
        raise RetrievalUnavailable("fastembed is not installed (uv sync --extra retrieval)") from exc
    return TextEmbedding(EMBED_MODEL, cache_dir=_cache_dir(), threads=2)


@lru_cache(maxsize=1)
def reranker() -> Any:
    try:
        from fastembed.rerank.cross_encoder import TextCrossEncoder
    except ImportError as exc:
        raise RetrievalUnavailable("fastembed is not installed (uv sync --extra retrieval)") from exc
    return TextCrossEncoder(RERANK_MODEL, cache_dir=_cache_dir(), threads=2)


def unit(vectors: "np.ndarray") -> "np.ndarray":
    np = _numpy()
    norms = np.linalg.norm(vectors, axis=-1, keepdims=True)
    return vectors / np.where(norms == 0, 1, norms)


def embed_queries(texts: list[str], model: Any = None) -> "np.ndarray":
    model = model or embedder()
    return unit(_numpy().array(list(model.query_embed(texts)), dtype="float32"))


def build(conn: sqlite3.Connection, model: Any = None, progress: Callable[[int, int], None] | None = None) -> int:
    """Embed every chunk and store the vectors. `conn` must be writable. Returns how many were stored."""
    np = _numpy()
    model = model or embedder()
    rows = conn.execute("SELECT id, title, heading, text FROM chunks ORDER BY id").fetchall()
    conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
    conn.execute(f"CREATE TABLE {TABLE} (chunk_id INTEGER PRIMARY KEY, vec BLOB NOT NULL)")
    step = 64
    for start in range(0, len(rows), step):
        batch = rows[start:start + step]
        matrix = unit(np.array(list(model.embed([embedding_text(r[1], r[2], r[3]) for r in batch], batch_size=16)),
                               dtype="float32"))
        conn.executemany(f"INSERT INTO {TABLE} (chunk_id, vec) VALUES (?, ?)",
                         [(r[0], matrix[i].tobytes()) for i, r in enumerate(batch)])
        if progress:
            progress(min(start + step, len(rows)), len(rows))
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('embedding_model', ?)", (EMBED_MODEL,))
    conn.commit()
    return len(rows)


def _db_key(conn: sqlite3.Connection) -> tuple[str, int, int]:
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    stat = os.stat(path) if path and Path(path).exists() else None
    return path, stat.st_mtime_ns if stat else 0, stat.st_size if stat else 0


@lru_cache(maxsize=4)
def _load(key: tuple[str, int, int]) -> tuple["np.ndarray", "np.ndarray"]:
    np = _numpy()
    conn = sqlite3.connect(f"file:{Path(key[0]).as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute(f"SELECT chunk_id, vec FROM {TABLE} ORDER BY chunk_id").fetchall()
    except sqlite3.Error as exc:
        raise RetrievalUnavailable(f"the database has no {TABLE} table (build it with --embeddings)") from exc
    finally:
        conn.close()
    if not rows:
        raise RetrievalUnavailable(f"the {TABLE} table is empty")
    ids = np.array([r[0] for r in rows], dtype="int64")
    return ids, np.stack([np.frombuffer(r[1], dtype="float32") for r in rows])


def load(conn: sqlite3.Connection) -> tuple["np.ndarray", "np.ndarray"]:
    """(chunk ids, unit vectors) for the database behind `conn`, cached until the file changes."""
    return _load(_db_key(conn))


def dense_ranked(conn: sqlite3.Connection, query: str, limit: int, model: Any = None) -> list[int]:
    """Chunk ids by cosine similarity to the query, best first."""
    np = _numpy()
    ids, matrix = load(conn)
    scores = matrix @ embed_queries([query], model)[0]
    top = np.argsort(-scores)[:limit]
    return [int(ids[i]) for i in top]


def rerank(query: str, candidates: list[tuple[int, str]], model: Any = None) -> list[int]:
    """Candidate chunk ids reordered by the cross-encoder, best first. `candidates` is (id, text)."""
    if not candidates:
        return []
    model = model or reranker()
    scores = list(model.rerank(query, [text for _, text in candidates]))
    order = sorted(range(len(candidates)), key=lambda i: -float(scores[i]))
    return [candidates[i][0] for i in order]
