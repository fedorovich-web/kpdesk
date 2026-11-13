"""Validation-only abstention thresholding for source-group-isolated ML ranking.

The model may rank candidates, but a low-confidence top candidate must not be
misrepresented as a verified SKU match. Thresholds are local experiment data.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Literal

from kpdesk.matching.artifact import TrustedModel
from kpdesk.matching.rank import rank_candidates
from kpdesk.matching.retrieve import retrieve_candidates
from kpdesk.matching.schema import CatalogProduct
from kpdesk.matching.training import LabeledQuery


@dataclass(frozen=True, slots=True)
class CaseScores:
    query_id: str
    relevant_skus: frozenset[str]
    ranked: tuple[tuple[str, float], ...]


@dataclass(frozen=True, slots=True)
class AbstentionMetrics:
    positive_queries: int
    no_match_queries: int
    positive_top1_correct: int
    positive_top1_wrong: int
    positive_abstained: int
    no_match_correct_empty: int
    no_match_false_suggestion: int
    balanced_top1_and_empty: float
    total_decisions_correct: int


def calculate_ranked_cases(
    queries: Sequence[LabeledQuery], catalog: Sequence[CatalogProduct], *,
    mode: Literal['ml', 'tfidf'], model: TrustedModel | None = None,
) -> tuple[CaseScores, ...]:
    if mode not in ("ml", "tfidf"):
        raise ValueError("Only explicitly comparable ML and TF-IDF modes supported.")
    return tuple(
        CaseScores(
            query_id=query.query_id,
            relevant_skus=query.relevant_skus,
            ranked=tuple((item.product.sku, item.score) for item in rank_candidates(
                query.request,
                retrieve_candidates(query.request, catalog, limit=20),
                mode=mode,
                artifact=model,
            )),
        )
        for query in queries
    )


def evaluate_abstention(
    cases: Sequence[CaseScores], threshold: float,
) -> AbstentionMetrics:
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or (
        not math.isfinite(threshold) or not 0 <= threshold <= 1
    ):
        raise ValueError("Threshold must be a finite probability within 0..1.")
    seen: set[str] = set()
    positive = negative = correct = wrong = missed = empty = false = 0
    for case in cases:
        if case.query_id in seen:
            raise ValueError("Duplicate query identifier during abstention evaluation.")
        seen.add(case.query_id)
        if case.ranked and (
            any(not math.isfinite(score) or not 0 <= score <= 1 for _, score in case.ranked)
            or len({sku for sku, _ in case.ranked}) != len(case.ranked)
        ):
            raise ValueError("Invalid or duplicated ranker scores.")
        best = case.ranked[0][0] if case.ranked and case.ranked[0][1] >= threshold else None
        if case.relevant_skus:
            positive += 1
            if best is None:
                missed += 1
            elif best in case.relevant_skus:
                correct += 1
            else:
                wrong += 1
        else:
            negative += 1
            if best is None:
                empty += 1
            else:
                false += 1
    balanced = (
        (correct / positive + empty / negative) / 2
        if positive and negative else 0.0
    )
    return AbstentionMetrics(
        positive_queries=positive,
        no_match_queries=negative,
        positive_top1_correct=correct,
        positive_top1_wrong=wrong,
        positive_abstained=missed,
        no_match_correct_empty=empty,
        no_match_false_suggestion=false,
        balanced_top1_and_empty=balanced,
        total_decisions_correct=correct + empty,
    )


def calibrate_threshold(validation: Sequence[CaseScores]) -> tuple[float, AbstentionMetrics]:
    """Select on validation only, independently for each ranking algorithm.

    Maximize macro-average of positive Hit@1 and correctly abstained no-match.
    Equal scores prefer fewer false suggestions, then smaller threshold
    (retain positive coverage). No test data or external source is consulted.
    """
    if not validation:
        raise ValueError("Validation set is empty.")
    if not any(case.relevant_skus for case in validation) or not any(
        not case.relevant_skus for case in validation
    ):
        raise ValueError("Calibration requires both positive and no-match validation examples.")
    cutoff_values = {0.0, 1.0}
    for case in validation:
        if case.ranked:
            score = case.ranked[0][1]
            if not 0 <= score <= 1 or not math.isfinite(score):
                raise ValueError("Invalid ranker top confidence.")
            cutoff_values.add(score)
            cutoff_values.add(min(1.0, math.nextafter(score, 1.0)))
    scores = [
        (cutoff, evaluate_abstention(validation, cutoff))
        for cutoff in sorted(cutoff_values)
    ]
    return max(scores, key=lambda item: (
        item[1].balanced_top1_and_empty,
        -item[1].no_match_false_suggestion,
        item[1].positive_top1_correct,
        -item[0],
    ))


def evidence_row(metrics: AbstentionMetrics) -> dict[str, float | int]:
    return asdict(metrics)
