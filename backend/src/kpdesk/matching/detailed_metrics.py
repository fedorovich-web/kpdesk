"""Positive-only ranking quality and separate no-match behavior.

Never count empty gold as a retrieval hit. Never inject correct SKU into retrieval.
Numbers are undefined as real-world performance without independent human labels.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from kpdesk.matching.rank import rank_candidates
from kpdesk.matching.retrieve import retrieve_candidates
from kpdesk.matching.schema import CatalogProduct, EvaluationCase
from kpdesk.matching.training import LabeledQuery


@dataclass(frozen=True, slots=True)
class DetailedRankingMetrics:
    k: int
    positive_queries: int
    no_match_queries: int
    hit_at_1: int
    hit_at_k: int
    mrr_at_k: float
    ndcg_at_k: float
    false_suggestions_on_no_match: int
    correct_empty_no_match: int
    unretrievable_positive_queries: int


def detailed_ranking_metrics(
    cases: Sequence[EvaluationCase],
    ranked_skus: Mapping[str, list[str]],
    *,
    k: int = 20,
) -> DetailedRankingMetrics:
    if not 1 <= k <= 20:
        raise ValueError("k must be within 1..20")
    seen: set[str] = set()
    positive = no_match = hit_one = hit_k = false_match = true_empty = 0
    reciprocal = ndcg = 0.0
    for case in cases:
        if case.query_id in seen:
            raise ValueError("Duplicate query_id")
        seen.add(case.query_id)
        ranked = ranked_skus.get(case.query_id, [])
        if len(set(ranked)) != len(ranked):
            raise ValueError("Duplicate predicted SKU")
        top = ranked[:k]
        if not case.relevant_skus:
            no_match += 1
            if top:
                false_match += 1
            else:
                true_empty += 1
            continue
        positive += 1
        if top and top[0] in case.relevant_skus:
            hit_one += 1
        first = next(
            (i for i, sku in enumerate(top, start=1) if sku in case.relevant_skus),
            None,
        )
        if first is not None:
            hit_k += 1
            reciprocal += 1.0 / first
        gain = sum(
            1.0 / math.log2(rank + 2)
            for rank, sku in enumerate(top)
            if sku in case.relevant_skus
        )
        ideal = sum(
            1.0 / math.log2(rank + 2)
            for rank in range(min(k, len(case.relevant_skus)))
        )
        ndcg += gain / ideal

    return DetailedRankingMetrics(
        k=k,
        positive_queries=positive,
        no_match_queries=no_match,
        hit_at_1=hit_one,
        hit_at_k=hit_k,
        mrr_at_k=reciprocal / positive if positive else 0.0,
        ndcg_at_k=ndcg / positive if positive else 0.0,
        false_suggestions_on_no_match=false_match,
        correct_empty_no_match=true_empty,
        unretrievable_positive_queries=positive - hit_k,
    )


def compare_lexical_baselines(
    queries: Sequence[LabeledQuery],
    products: Mapping[str, CatalogProduct],
    *,
    max_candidates: int = 20,
) -> dict[str, DetailedRankingMetrics]:
    """Compare same retrieval set; compare metrics, not incomparable gold-injected sets."""
    cases = [EvaluationCase(q.query_id, q.relevant_skus) for q in queries]
    all_predictions: dict[str, dict[str, list[str]]] = {
        "rapidfuzz": {},
        "tfidf": {},
    }
    catalog = list(products.values())
    for query in queries:
        retrieved = retrieve_candidates(query.request, catalog, max_candidates)
        for mode in ("rapidfuzz", "tfidf"):
            predictions = rank_candidates(query.request, retrieved, mode)
            all_predictions[mode][query.query_id] = [
                item.product.sku for item in predictions
            ]
    return {
        mode: detailed_ranking_metrics(cases, predictions, k=max_candidates)
        for mode, predictions in all_predictions.items()
    }
