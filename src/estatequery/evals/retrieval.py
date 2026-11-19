"""Retrieval evaluation: how well does each way of searching DarGlobal's passages find the right ones?

`eval/retrieval.jsonl` holds queries, each with `relevant_when`: facts of the answer, as alternatives
of term lists. A passage is relevant when it contains every term of at least one alternative. The
terms describe the answer, not the query, so a paraphrased question is not graded by how many of
its own words a passage happens to share.

Four rankings are compared on the same queries: BM25 (what the app uses), dense embeddings,
hybrid (reciprocal rank fusion of the two) and hybrid with a cross-encoder reranking the best few.
The run works on a temporary copy of the database, so the real one is never modified.

    uv run estatequery eval-retrieval        needs: uv sync --extra retrieval
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import statistics
import tempfile
import time
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Callable

from estatequery.config import get_settings
from estatequery.retrieval import vectors
from estatequery.retrieval.metrics import hit_at_k, ndcg_at_k, precision_at_k, recall_at_k, reciprocal_rank
from estatequery.retrieval.search import bm25_ranked, hybrid_ranked, ranked_ids

QUERIES_PATH = Path("eval/retrieval.jsonl")
RESULTS_MD = Path("eval/retrieval-results.md")
DEPTH = 10            # rankings are taken ten deep; the tool returns the first four by default
SHOWN = 4
RERANK_CANDIDATES = 8
SHIP_MARGIN = 0.05    # a mode must beat BM25 by this much on MRR or hit@4 to be worth its cost


def load_queries(path: Path = QUERIES_PATH) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def relevant_ids(conn: sqlite3.Connection, alternatives: list[list[str]]) -> set[int]:
    """Passages that contain every term of at least one alternative (case-insensitive)."""
    found: set[int] = set()
    for chunk_id, text in conn.execute("SELECT id, text FROM chunks"):
        lowered = (text or "").lower()
        if any(all(term.lower() in lowered for term in alt) for alt in alternatives):
            found.add(chunk_id)
    return found


def score(ranked: list[int], relevant: set[int]) -> dict[str, float | None]:
    return {
        f"P@{SHOWN}": precision_at_k(ranked, relevant, SHOWN),
        f"Hit@{SHOWN}": hit_at_k(ranked, relevant, SHOWN),
        f"Recall@{DEPTH}": recall_at_k(ranked, relevant, DEPTH),
        f"MRR@{DEPTH}": reciprocal_rank(ranked, relevant, DEPTH),
        f"NDCG@{DEPTH}": ndcg_at_k(ranked, relevant, DEPTH),
    }


def first_rank(ranked: list[int], relevant: set[int]) -> int | None:
    return next((i for i, item in enumerate(ranked, start=1) if item in relevant), None)


def mean(values: list[float | None]) -> float | None:
    clean = [v for v in values if v is not None]
    return statistics.mean(clean) if clean else None


def compare(conn: sqlite3.Connection, queries: list[dict[str, Any]],
            modes: dict[str, Callable[[sqlite3.Connection, str], list[int]]]) -> dict[str, Any]:
    """Run every mode on every query. Returns per-mode metrics, latencies and per-query first ranks."""
    relevant = {q["id"]: relevant_ids(conn, q["relevant_when"]) for q in queries}
    out: dict[str, Any] = {"relevant": {k: len(v) for k, v in relevant.items()}, "modes": {}}
    for name, search in modes.items():
        per_query: dict[str, dict[str, float | None]] = {}
        ranks: dict[str, int | None] = {}
        latencies: list[float] = []
        for q in queries:
            started = time.perf_counter()
            ranked = search(conn, q["query"])
            latencies.append((time.perf_counter() - started) * 1000)
            per_query[q["id"]] = score(ranked, relevant[q["id"]])
            ranks[q["id"]] = first_rank(ranked, relevant[q["id"]])
        metrics = list(next(iter(per_query.values())))
        by_kind: dict[str, dict[str, float | None]] = {}
        for kind in dict.fromkeys(q["kind"] for q in queries):
            ids = [q["id"] for q in queries if q["kind"] == kind]
            by_kind[kind] = {m: mean([per_query[i][m] for i in ids]) for m in metrics}
        out["modes"][name] = {
            "overall": {m: mean([p[m] for p in per_query.values()]) for m in metrics},
            "by_kind": by_kind,
            "median_ms": statistics.median(latencies),
            "p95_ms": sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)],
            "first_rank": ranks,
        }
    return out


def fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def report(result: dict[str, Any], queries: list[dict[str, Any]], n_chunks: int) -> list[str]:
    modes = result["modes"]
    metrics = list(next(iter(modes.values()))["overall"])
    lines = [
        "# Retrieval evaluation", "",
        f"Run on {date.today().isoformat()} over {len(queries)} labelled queries and {n_chunks} DarGlobal passages. "
        f"Embeddings `{vectors.EMBED_MODEL}`, reranker `{vectors.RERANK_MODEL}` (top {RERANK_CANDIDATES} reranked). "
        "Regenerate with `uv run estatequery eval-retrieval`.", "",
        "A passage is relevant when it contains the answer terms listed in `eval/retrieval.jsonl` (the facts of the "
        "answer, not the words of the query).", "",
        "| Mode | " + " | ".join(metrics) + " | Median ms | p95 ms |",
        "|---|" + "---|" * (len(metrics) + 2),
    ]
    for name, m in modes.items():
        lines.append(f"| {name} | " + " | ".join(fmt(m['overall'][k]) for k in metrics)
                     + f" | {m['median_ms']:.0f} | {m['p95_ms']:.0f} |")
    for kind in dict.fromkeys(q["kind"] for q in queries):
        n = sum(q["kind"] == kind for q in queries)
        lines += ["", f"### {kind.capitalize()} queries ({n})", "",
                  "| Mode | " + " | ".join(metrics) + " |", "|---|" + "---|" * len(metrics)]
        for name, m in modes.items():
            lines.append(f"| {name} | " + " | ".join(fmt(m['by_kind'][kind][k]) for k in metrics) + " |")
    lines += ["", "### Rank of the first relevant passage per query", "",
              "| Query | Relevant passages | " + " | ".join(modes) + " |", "|---|---|" + "---|" * len(modes)]
    for q in queries:
        ranks = " | ".join(str(m["first_rank"][q["id"]] or "-") for m in modes.values())
        lines.append(f"| {q['id']} ({q['kind']}): {q['query']} | {result['relevant'][q['id']]} | {ranks} |")
    base = modes["bm25"]["overall"]
    lines += ["", "### Against BM25", ""]
    for name, m in modes.items():
        if name == "bm25":
            continue
        d_mrr = m["overall"][f"MRR@{DEPTH}"] - base[f"MRR@{DEPTH}"]
        d_hit = m["overall"][f"Hit@{SHOWN}"] - base[f"Hit@{SHOWN}"]
        worth = d_mrr >= SHIP_MARGIN or d_hit >= SHIP_MARGIN
        lines.append(f"- **{name}**: MRR {d_mrr:+.2f}, Hit@{SHOWN} {d_hit:+.2f}, "
                     f"{m['median_ms'] - modes['bm25']['median_ms']:+.0f} ms median. "
                     f"{'Clears' if worth else 'Does not clear'} the {SHIP_MARGIN:.2f} margin on MRR or Hit@{SHOWN}.")
    lines += ["", "Latency is measured on the machine that ran this, not on the server; a 2-vCPU host is slower. "
              "Small differences between modes on 23 queries are not significant."]
    return lines


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Compare BM25, dense, hybrid and reranked retrieval.")
    parser.add_argument("--no-rerank", action="store_true", help="skip the cross-encoder mode")
    args = parser.parse_args(argv)
    queries = load_queries()
    source = get_settings().db_path
    with tempfile.TemporaryDirectory() as scratch:
        db = Path(scratch) / "estatequery.db"
        shutil.copy(source, db)
        conn = sqlite3.connect(db)
        print(f"embedding passages with {vectors.EMBED_MODEL} (about a minute) ...", flush=True)
        n = vectors.build(conn, progress=lambda done, total: print(f"  {done}/{total}", end="\r", flush=True))
        conn.close()
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        modes: dict[str, Callable[[sqlite3.Connection, str], list[int]]] = {
            "bm25": lambda c, q: bm25_ranked(c, q, DEPTH),
            "dense": lambda c, q: vectors.dense_ranked(c, q, DEPTH),
            "hybrid": lambda c, q: hybrid_ranked(c, q, DEPTH),
        }
        if not args.no_rerank:
            modes["hybrid+rerank"] = lambda c, q: ranked_ids(
                c, q, DEPTH, mode="hybrid", rerank=True, rerank_candidates=RERANK_CANDIDATES)[0]
        for search in modes.values():           # load the models before anything is timed
            search(conn, queries[0]["query"])
        result = compare(conn, queries, modes)
        conn.close()
    lines = report(result, queries, n)
    RESULTS_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
