"""Ranking metrics over independently supplied, grouped gold labels."""

from collections.abc import Mapping, Sequence

from kpdesk.matching.schema import EvaluationCase, RankingMetrics


def evaluate_ranking(
    cases: Sequence[EvaluationCase],
    ranked_skus: Mapping[str, list[str]],
    k: int = 20,
) -> RankingMetrics:
    if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= 20:
        raise ValueError("k must be within 1..20")

    known_ids: set[str] = set()
    positive = no_match = retrieval_hits = hit_one = hit_three = 0
    false_suggestions = correct_empty = 0
    for case in cases:
        if case.query_id in known_ids:
            raise ValueError(f"Duplicate evaluation query id: {case.query_id}")
        known_ids.add(case.query_id)
        retrieved = ranked_skus.get(case.query_id, [])
        if len(set(retrieved)) != len(retrieved):
            raise ValueError('Duplicate predicted SKU')
        if not case.relevant_skus:
            no_match += 1
            false_suggestions += bool(retrieved[:k])
            correct_empty += not retrieved[:k]
            continue
        positive += 1
        retrieval_hits += bool(case.relevant_skus.intersection(retrieved[:k]))
        hit_one += bool(case.relevant_skus.intersection(retrieved[:1]))
        hit_three += bool(case.relevant_skus.intersection(retrieved[:3]))

    return RankingMetrics(
        total_queries=len(cases),
        positive_queries=positive,
        no_match_queries=no_match,
        retrieval_hits=retrieval_hits,
        hit_at_1=hit_one,
        hit_at_3=hit_three,
        unretrievable_positive_queries=positive - retrieval_hits,
        false_suggestions_on_no_match=false_suggestions,
        correct_empty_no_match=correct_empty,
    )
