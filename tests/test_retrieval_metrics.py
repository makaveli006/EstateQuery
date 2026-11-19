"""Ranking metrics checked against values worked out by hand."""

from __future__ import annotations

import math

import pytest

from estatequery.retrieval.metrics import (
    hit_at_k,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    reciprocal_rank_fusion,
)

RANKED = ["a", "b", "c", "d", "e"]


def test_precision_counts_relevant_results_in_the_first_k_slots() -> None:
    assert precision_at_k(RANKED, {"a", "c", "x"}, 4) == 0.5            # a and c in the first four
    assert precision_at_k(RANKED, {"a"}, 1) == 1.0
    assert precision_at_k(["a"], {"a"}, 4) == 0.25                      # three empty slots are misses
    assert precision_at_k([], {"a"}, 4) == 0.0


def test_recall_is_the_share_of_all_relevant_results_found() -> None:
    assert recall_at_k(RANKED, {"a", "c", "x"}, 4) == pytest.approx(2 / 3)
    assert recall_at_k(RANKED, {"x"}, 5) == 0.0
    assert recall_at_k(RANKED, set(), 5) is None


def test_hit_is_one_when_anything_relevant_is_in_the_first_k() -> None:
    assert hit_at_k(RANKED, {"d"}, 4) == 1.0 and hit_at_k(RANKED, {"e"}, 4) == 0.0
    assert hit_at_k([], {"a"}, 4) == 0.0


def test_reciprocal_rank_uses_the_first_relevant_position() -> None:
    assert reciprocal_rank(RANKED, {"c", "e"}) == pytest.approx(1 / 3)
    assert reciprocal_rank(RANKED, {"a"}) == 1.0
    assert reciprocal_rank(RANKED, {"zz"}) == 0.0
    assert reciprocal_rank(RANKED, {"e"}, k=3) == 0.0                    # beyond the cut-off


def test_ndcg_rewards_relevant_results_near_the_top() -> None:
    perfect = ndcg_at_k(["a", "b", "x"], {"a", "b"}, 3)
    assert perfect == pytest.approx(1.0)
    late = ndcg_at_k(["x", "a", "b"], {"a", "b"}, 3)
    expected = (1 / math.log2(3) + 1 / math.log2(4)) / (1 / math.log2(2) + 1 / math.log2(3))
    assert late == pytest.approx(expected) and late < perfect
    assert ndcg_at_k(RANKED, {"zz"}, 5) == 0.0
    assert ndcg_at_k(RANKED, set(), 5) is None


def test_ndcg_ideal_is_capped_by_k() -> None:
    assert ndcg_at_k(["a", "b"], {"a", "b", "c", "d", "e"}, 2) == pytest.approx(1.0)


def test_fusion_rewards_agreement_between_lists() -> None:
    bm25 = ["a", "b", "c"]
    dense = ["c", "a", "d"]
    fused = reciprocal_rank_fusion([bm25, dense])
    assert fused[:2] == ["a", "c"]                                       # both lists have them near the top
    assert set(fused) == {"a", "b", "c", "d"}
    assert fused.index("b") < fused.index("d") or fused.index("d") < fused.index("b")


def test_fusion_scores_follow_the_formula_and_handle_edge_cases() -> None:
    fused = reciprocal_rank_fusion([["a", "b"], ["b"]], k=60)
    assert fused == ["b", "a"]                                           # b: 1/62 + 1/61 beats a: 1/61
    assert reciprocal_rank_fusion([]) == [] and reciprocal_rank_fusion([[], []]) == []
    assert reciprocal_rank_fusion([["x", "y"]]) == ["x", "y"]            # one list keeps its order
