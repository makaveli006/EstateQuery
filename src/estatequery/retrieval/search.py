"""Passage retrieval for `search_darglobal`: BM25, dense, hybrid (reciprocal rank fusion), reranked.

`ranked_ids` is what the tool calls. In the default mode (`bm25`) it behaves exactly as the tool always
did. In `hybrid` mode it fuses the BM25 and dense rankings and, optionally, reorders the best few with
a cross-encoder. Any problem with the dense side (library, models or vectors missing) logs a warning
and returns the BM25 ranking, so retrieval never fails because of the optional parts.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from estatequery.retrieval import vectors
from estatequery.retrieval.metrics import reciprocal_rank_fusion

log = logging.getLogger("estatequery.retrieval")
CANDIDATES = 20            # how many each ranking contributes to the fusion
WARMUP_STARTED = threading.Event()   # set when the app began loading the models in the background
WARMUP_READY = threading.Event()     # set when they finished; until then, and if they fail, searches use BM25
WARMUP_FAILED = threading.Event()


def bm25_ranked(conn: sqlite3.Connection, query: str, limit: int) -> list[int]:
    """Chunk ids by BM25 over title, heading and text: all the words first, any of them as a fallback."""
    from estatequery.agent.tools import fts_query   # imported here: tools.py imports this module

    for joiner in ("AND", "OR"):
        match = fts_query(query, joiner)
        if not match:
            break
        rows = conn.execute(
            "SELECT c.id FROM chunks_fts f JOIN chunks c ON c.id = f.rowid "
            "WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts, 4.0, 2.0, 1.0) LIMIT ?",
            (match, limit),
        ).fetchall()
        if rows:
            return [r[0] for r in rows]
    return []


def hybrid_ranked(conn: sqlite3.Connection, query: str, limit: int, *, embed_model: Any = None) -> list[int]:
    """BM25 and dense rankings merged by reciprocal rank fusion."""
    lexical = bm25_ranked(conn, query, CANDIDATES)
    dense = vectors.dense_ranked(conn, query, CANDIDATES, embed_model)
    return [int(i) for i in reciprocal_rank_fusion([lexical, dense])][:limit]


def rerank_ids(conn: sqlite3.Connection, query: str, ids: list[int], *, rerank_model: Any = None) -> list[int]:
    marks = ",".join("?" for _ in ids)
    rows = {r[0]: vectors.rerank_text(r[1], r[2]) for r in conn.execute(
        f"SELECT id, title, text FROM chunks WHERE id IN ({marks})", ids)}
    return vectors.rerank(query, [(i, rows[i]) for i in ids if i in rows], rerank_model)


def ranked_ids(conn: sqlite3.Connection, query: str, limit: int, *, mode: str = "bm25", rerank: bool = False,
               rerank_candidates: int = 8, embed_model: Any = None, rerank_model: Any = None) -> tuple[list[int], str]:
    """(chunk ids best first, the mode actually used)."""
    if mode != "hybrid" or (WARMUP_STARTED.is_set() and not WARMUP_READY.is_set()):
        return bm25_ranked(conn, query, limit), "bm25"
    try:
        ids = hybrid_ranked(conn, query, max(limit, rerank_candidates if rerank else limit), embed_model=embed_model)
        if rerank:
            head = rerank_ids(conn, query, ids[:rerank_candidates], rerank_model=rerank_model)
            ids = head + [i for i in ids if i not in head]
        return ids[:limit], "hybrid+rerank" if rerank else "hybrid"
    except Exception as exc:  # noqa: BLE001 - the optional parts must never break a chat turn
        log.warning("hybrid retrieval failed (%s: %s), using BM25", type(exc).__name__, exc)
        return bm25_ranked(conn, query, limit), "bm25"


def warm_up(db_path: Path, *, rerank: bool = False) -> bool:
    """Load the vectors and models now, so the first visitor's search does not pay for it (about
    ten seconds). Returns whether hybrid retrieval is ready; failures are logged, never raised."""
    WARMUP_STARTED.set()
    started = time.monotonic()
    try:
        conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
        try:
            vectors.dense_ranked(conn, "warm up", 1)
        finally:
            conn.close()
        if rerank:
            vectors.reranker()
        WARMUP_READY.set()
        log.info("hybrid retrieval ready in %.1f s", time.monotonic() - started)
        return True
    except Exception as exc:  # noqa: BLE001
        WARMUP_FAILED.set()
        log.warning("hybrid retrieval warm-up failed (%s: %s); searches will use BM25", type(exc).__name__, exc)
        return False


def status(mode: str) -> str:
    """What the next search will use, for the health check."""
    if mode != "hybrid":
        return "bm25"
    if WARMUP_READY.is_set():
        return "hybrid"
    return "bm25 (hybrid unavailable)" if WARMUP_FAILED.is_set() else "bm25 (hybrid warming up)"
