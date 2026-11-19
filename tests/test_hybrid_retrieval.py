"""Hybrid retrieval plumbing with a tiny fake embedder and reranker (no models, no network)."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")   # installed with the `retrieval` extra

from estatequery.agent.tools import run_tool
from estatequery.config import get_settings
from estatequery.ingest.build_db import build, main as build_main
from estatequery.retrieval import search, vectors
from estatequery.retrieval.search import bm25_ranked, hybrid_ranked, ranked_ids

FIXTURES = Path(__file__).parent / "fixtures" / "raw"
DIM = 64


def words(text: str) -> list[str]:
    return [w for w in "".join(c.lower() if c.isalnum() else " " for c in text).split() if len(w) > 2]


class FakeEmbedder:
    """Bag-of-words hashing: texts that share words are close. Deterministic, and no download."""

    def _vector(self, text: str) -> np.ndarray:
        v = np.zeros(DIM, dtype=np.float32)
        for w in words(text):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % DIM] += 1
        return v

    def embed(self, texts, batch_size: int = 16):
        return (self._vector(t) for t in texts)

    query_embed = embed


class FakeReranker:
    """Scores a passage by how many query words it contains."""

    def rerank(self, query: str, texts: list[str]):
        q = set(words(query))
        return [float(len(q & set(words(t)))) for t in texts]


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch: pytest.MonkeyPatch):
    vectors._load.cache_clear()
    search.WARMUP_STARTED.clear()
    search.WARMUP_READY.clear()
    search.WARMUP_FAILED.clear()
    monkeypatch.setattr(vectors, "embedder", lambda: FakeEmbedder())
    monkeypatch.setattr(vectors, "reranker", lambda: FakeReranker())
    yield
    vectors._load.cache_clear()
    search.WARMUP_STARTED.clear()
    search.WARMUP_READY.clear()
    search.WARMUP_FAILED.clear()


@pytest.fixture()
def vec_db(tmp_path: Path) -> Path:
    """A fixture database with passage vectors built by the fake embedder."""
    path = tmp_path / "estatequery.db"
    build(FIXTURES, path, snapshot="2026-11-19")
    conn = sqlite3.connect(path)
    vectors.build(conn)
    conn.close()
    return path


def open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def use_hybrid(monkeypatch: pytest.MonkeyPatch, rerank: bool = False) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "retrieval_mode", "hybrid")
    monkeypatch.setattr(settings, "retrieval_rerank", rerank)


# ---------------------------------------------------------------------------- storage

def test_building_stores_one_unit_vector_per_passage_and_names_the_model(vec_db: Path) -> None:
    conn = open_ro(vec_db)
    chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert conn.execute(f"SELECT COUNT(*) FROM {vectors.TABLE}").fetchone()[0] == chunks > 0
    assert conn.execute("SELECT value FROM meta WHERE key = 'embedding_model'").fetchone()[0] == vectors.EMBED_MODEL
    ids, matrix = vectors.load(conn)
    assert matrix.shape == (chunks, DIM) and np.allclose(np.linalg.norm(matrix, axis=1), 1.0)
    assert list(ids) == sorted(ids)


def test_rebuilding_replaces_the_vectors(vec_db: Path) -> None:
    conn = sqlite3.connect(vec_db)
    first = vectors.build(conn)
    assert vectors.build(conn) == first and conn.execute(f"SELECT COUNT(*) FROM {vectors.TABLE}").fetchone()[0] == first


def test_the_embedding_text_carries_the_page_title_and_heading() -> None:
    text = vectors.embedding_text("Trump Tower Jeddah", "Location", "Ten minutes from the airport.")
    assert text.startswith("Trump Tower Jeddah. Location. Ten")
    assert len(vectors.embedding_text(None, None, "x" * 5000)) == vectors.MAX_EMBED_CHARS


def test_dense_ranking_puts_the_passage_sharing_the_query_words_first(vec_db: Path) -> None:
    conn = open_ro(vec_db)
    chunk_id, title, heading, text = conn.execute("SELECT id, title, heading, text FROM chunks LIMIT 1").fetchone()
    assert vectors.dense_ranked(conn, text, 3)[0] == chunk_id


def test_a_database_without_vectors_is_reported_as_unavailable(tmp_path: Path) -> None:
    path = tmp_path / "plain.db"
    build(FIXTURES, path, snapshot="2026-11-19")
    with pytest.raises(vectors.RetrievalUnavailable, match="chunk_vectors"):
        vectors.dense_ranked(open_ro(path), "anything", 3)


# ---------------------------------------------------------------------------- ranking

def test_bm25_ranking_is_what_the_tool_always_returned(vec_db: Path) -> None:
    conn = open_ro(vec_db)
    ids = bm25_ranked(conn, "tower", 5)
    assert ids and ids == bm25_ranked(conn, "tower", 5)
    assert bm25_ranked(conn, "the of and", 5) == []           # nothing but stop words


def test_hybrid_merges_both_rankings(vec_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_ro(vec_db)
    monkeypatch.setattr(search, "bm25_ranked", lambda c, q, n: [1, 2, 3])
    monkeypatch.setattr(vectors, "dense_ranked", lambda c, q, n, m=None: [3, 4, 1])
    assert hybrid_ranked(conn, "q", 10) == [1, 3, 2, 4]        # 1 and 3 are in both lists
    assert hybrid_ranked(conn, "q", 2) == [1, 3]


def test_ranked_ids_reports_the_mode_that_actually_ran(vec_db: Path) -> None:
    conn = open_ro(vec_db)
    assert ranked_ids(conn, "tower", 4)[1] == "bm25"
    ids, mode = ranked_ids(conn, "tower", 4, mode="hybrid")
    assert mode == "hybrid" and 0 < len(ids) <= 4
    ids, mode = ranked_ids(conn, "tower", 4, mode="hybrid", rerank=True, rerank_candidates=3)
    assert mode == "hybrid+rerank" and 0 < len(ids) <= 4


def test_reranking_reorders_the_best_candidates_and_keeps_the_rest(vec_db: Path, monkeypatch) -> None:
    conn = open_ro(vec_db)
    monkeypatch.setattr(search, "hybrid_ranked", lambda *a, **k: [5, 6, 7, 8, 9])
    monkeypatch.setattr(search, "rerank_ids", lambda c, q, ids, **k: list(reversed(ids)))
    ids, mode = ranked_ids(conn, "q", 5, mode="hybrid", rerank=True, rerank_candidates=3)
    assert ids == [7, 6, 5, 8, 9] and mode == "hybrid+rerank"


def test_the_fake_reranker_scores_passages_by_query_words(vec_db: Path) -> None:
    conn = open_ro(vec_db)
    ids = [r[0] for r in conn.execute("SELECT id FROM chunks LIMIT 6")]
    text = conn.execute("SELECT text FROM chunks WHERE id = ?", (ids[3],)).fetchone()[0]
    assert search.rerank_ids(conn, text, ids)[0] == ids[3]


@pytest.mark.parametrize("failure", [vectors.RetrievalUnavailable("no vectors"), RuntimeError("onnx exploded"),
                                     MemoryError("out of memory")])
def test_any_failure_of_the_dense_side_falls_back_to_bm25(vec_db: Path, monkeypatch, failure) -> None:
    conn = open_ro(vec_db)

    def boom(*a, **k):
        raise failure

    monkeypatch.setattr(vectors, "dense_ranked", boom)
    ids, mode = ranked_ids(conn, "tower", 4, mode="hybrid")
    assert mode == "bm25" and ids == bm25_ranked(conn, "tower", 4)


def test_hybrid_without_vectors_still_answers_from_bm25(tmp_path: Path) -> None:
    path = tmp_path / "plain.db"
    build(FIXTURES, path, snapshot="2026-11-19")
    conn = open_ro(path)
    ids, mode = ranked_ids(conn, "tower", 4, mode="hybrid")
    assert mode == "bm25" and ids == bm25_ranked(conn, "tower", 4)


# ---------------------------------------------------------------------------- warm-up

def test_until_the_models_are_loaded_searches_use_bm25_without_waiting(vec_db: Path) -> None:
    conn = open_ro(vec_db)
    search.WARMUP_STARTED.set()
    assert ranked_ids(conn, "tower", 4, mode="hybrid")[1] == "bm25"
    search.WARMUP_READY.set()
    assert ranked_ids(conn, "tower", 4, mode="hybrid")[1] == "hybrid"


def test_warm_up_marks_ready_on_success_and_stays_on_bm25_after_a_failure(vec_db: Path, tmp_path: Path) -> None:
    assert search.warm_up(vec_db) is True and search.WARMUP_READY.is_set()
    search.WARMUP_READY.clear()
    plain = tmp_path / "plain.db"
    build(FIXTURES, plain, snapshot="2026-11-19")
    assert search.warm_up(plain) is False and not search.WARMUP_READY.is_set() and search.WARMUP_STARTED.is_set()
    assert ranked_ids(open_ro(vec_db), "tower", 4, mode="hybrid")[1] == "bm25"   # failed warm-up: BM25 for good


# ---------------------------------------------------------------------------- the tool

def test_search_darglobal_is_unchanged_in_bm25_mode(vec_db: Path) -> None:
    result = run_tool("search_darglobal", {"query": "tower"}, vec_db)
    assert "retrieval" not in result and result["passages"] and set(result["passages"][0]) == {"title", "heading", "text", "url"}


def test_search_darglobal_in_hybrid_mode_returns_the_same_shape_and_says_how(vec_db: Path, monkeypatch) -> None:
    use_hybrid(monkeypatch)
    result = run_tool("search_darglobal", {"query": "tower", "limit": 3}, vec_db)
    assert result["retrieval"] == "hybrid" and 0 < result["returned"] <= 3
    assert set(result["passages"][0]) == {"title", "heading", "text", "url"} and "error" not in result


def test_search_darglobal_with_rerank_and_without_vectors(vec_db: Path, tmp_path: Path, monkeypatch) -> None:
    use_hybrid(monkeypatch, rerank=True)
    assert run_tool("search_darglobal", {"query": "tower"}, vec_db)["retrieval"] == "hybrid+rerank"
    plain = tmp_path / "plain.db"
    build(FIXTURES, plain, snapshot="2026-11-19")
    result = run_tool("search_darglobal", {"query": "tower"}, plain)
    assert "retrieval" not in result and result["passages"]            # fell back to BM25 and still answered


def test_a_query_with_no_lexical_match_still_gets_passages_from_the_dense_side(vec_db: Path, monkeypatch) -> None:
    conn = open_ro(vec_db)
    text = conn.execute("SELECT text FROM chunks WHERE id = 3").fetchone()[0]
    query = " ".join(sorted(set(words(text)))[:6]) + " zzzzunknownword"          # an unmatched word forces the AND to fail
    use_hybrid(monkeypatch)
    assert run_tool("search_darglobal", {"query": query}, vec_db)["returned"] > 0


# ---------------------------------------------------------------------------- build command

def test_build_db_embeddings_flag_stores_vectors(tmp_path: Path) -> None:
    db = tmp_path / "estatequery.db"
    build_main(["--raw-dir", str(FIXTURES), "--db", str(db), "--snapshot", "2026-11-19", "--embeddings"])
    conn = sqlite3.connect(db)
    assert conn.execute(f"SELECT COUNT(*) FROM {vectors.TABLE}").fetchone()[0] == conn.execute(
        "SELECT COUNT(*) FROM chunks").fetchone()[0] > 0


def test_build_db_without_the_flag_adds_no_vectors(tmp_path: Path) -> None:
    db = tmp_path / "estatequery.db"
    build_main(["--raw-dir", str(FIXTURES), "--db", str(db), "--snapshot", "2026-11-19"])
    names = {r[0] for r in sqlite3.connect(db).execute("SELECT name FROM sqlite_master")}
    assert vectors.TABLE not in names


def test_the_health_check_says_what_the_next_search_will_use(vec_db: Path, tmp_path: Path) -> None:
    assert search.status("bm25") == "bm25"
    assert search.status("hybrid") == "bm25 (hybrid warming up)"
    assert search.warm_up(vec_db) is True
    assert search.status("hybrid") == "hybrid" and search.status("bm25") == "bm25"
    search.WARMUP_READY.clear()
    plain = tmp_path / "plain.db"
    build(FIXTURES, plain, snapshot="2026-11-19")
    assert search.warm_up(plain) is False
    assert search.status("hybrid") == "bm25 (hybrid unavailable)"
