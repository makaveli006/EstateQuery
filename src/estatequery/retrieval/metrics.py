"""Ranking metrics and rank fusion. Pure functions over lists of ids, so they are easy to check by hand.

`ranked` is the list of retrieved ids, best first; `relevant` is the set of ids that count as right.
"""

from __future__ import annotations

import math
from typing import Hashable, Sequence


def precision_at_k(ranked: Sequence[Hashable], relevant: set[Hashable], k: int) -> float:
    """Share of the first k slots that hold a relevant result. A short list counts its empty slots as misses."""
    return sum(item in relevant for item in ranked[:k]) / k


def recall_at_k(ranked: Sequence[Hashable], relevant: set[Hashable], k: int) -> float | None:
    """Share of all relevant results found in the first k. None when nothing is relevant."""
    if not relevant:
        return None
    return len(set(ranked[:k]) & relevant) / len(relevant)


def hit_at_k(ranked: Sequence[Hashable], relevant: set[Hashable], k: int) -> float:
    """1 when at least one relevant result is in the first k (the answer was reachable), else 0."""
    return float(any(item in relevant for item in ranked[:k]))


def reciprocal_rank(ranked: Sequence[Hashable], relevant: set[Hashable], k: int = 10) -> float:
    """1 / rank of the first relevant result within k, or 0 (the mean over queries is the MRR)."""
    for position, item in enumerate(ranked[:k], start=1):
        if item in relevant:
            return 1 / position
    return 0.0


def ndcg_at_k(ranked: Sequence[Hashable], relevant: set[Hashable], k: int) -> float | None:
    """Normalised discounted cumulative gain with binary relevance. None when nothing is relevant."""
    if not relevant:
        return None
    gain = sum(1 / math.log2(position + 2) for position, item in enumerate(ranked[:k]) if item in relevant)
    best = sum(1 / math.log2(position + 2) for position in range(min(len(relevant), k)))
    return gain / best


def reciprocal_rank_fusion(rankings: Sequence[Sequence[Hashable]], k: int = 60) -> list[Hashable]:
    """Merge several ranked lists: each item scores the sum of 1 / (k + rank) over the lists it is in.

    It uses ranks, not scores, so BM25 and cosine similarity can be combined without scaling. Ties
    keep the order in which the item first appeared.
    """
    scores: dict[Hashable, float] = {}
    for ranking in rankings:
        for position, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1 / (k + position)
    return sorted(scores, key=lambda item: -scores[item])
