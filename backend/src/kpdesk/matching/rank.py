"""Candidate scoring with explicit mode and hard compatibility filters."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Literal

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from kpdesk.matching.artifact import TrustedModel
from kpdesk.matching.constraints import check_constraints
from kpdesk.matching.features import FEATURE_SCHEMA, PairInput
from kpdesk.matching.normalize import normalize_text
from kpdesk.matching.schema import Candidate, RequestedItem


def _tfidf_scores(request: RequestedItem, candidates: Sequence[Candidate]) -> list[float]:
    texts = [normalize_text(request.description)]
    texts.extend(normalize_text(item.product.name) for item in candidates)
    vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4))
    encoded = vectorizer.fit_transform(texts)
    similarities = encoded[1:].multiply(encoded[0]).sum(axis=1)
    values = np.asarray(similarities).reshape(-1)
    # Nonnegative normalized TF-IDF vectors have cosine in [0, 1]. Summation
    # roundoff for identical vectors can exceed 1 by an epsilon, breaking the API.
    return [min(1.0, max(0.0, float(value))) for value in values]


def rank_candidates(
    request: RequestedItem,
    candidates: Sequence[Candidate],
    mode: Literal["rapidfuzz", "tfidf", "ml"],
    artifact: TrustedModel | None = None,
    *,
    min_score: float | None = None,
) -> list[Candidate]:
    """An ML model may rank compatible products, but never override mandatory constraints."""
    if mode not in ("rapidfuzz", "tfidf", "ml"):
        raise ValueError("unsupported_matching_mode")
    if min_score is not None and (
        isinstance(min_score, bool) or not isinstance(min_score, (int, float))
        or not math.isfinite(min_score) or not 0 <= min_score <= 1
    ):
        raise ValueError("min_score must be a finite normalized confidence in 0..1")
    if mode == "ml" and artifact is None:
        raise ValueError("model_unavailable")
    if mode == "ml" and artifact is not None and artifact.feature_schema != FEATURE_SCHEMA:
        raise ValueError("model_schema_mismatch")

    eligible: list[Candidate] = []
    for candidate in candidates:
        decision = check_constraints(request, candidate.product)
        if decision.status != "incompatible":
            eligible.append(Candidate(candidate.product, decision, candidate.score))
    if not eligible:
        return []

    if mode == "rapidfuzz":
        values = [candidate.score for candidate in eligible]
    elif mode == "tfidf":
        values = _tfidf_scores(request, eligible)
    else:
        if artifact is None:
            raise ValueError("model_unavailable")
        pairs = [PairInput(request, candidate.product) for candidate in eligible]
        values = [
            float(value)
            for value in artifact.pipeline.predict_proba(pairs)[:, 1]
        ]

    scored = [
        Candidate(candidate.product, candidate.constraints, float(score))
        for candidate, score in zip(eligible, values, strict=True)
    ]
    scored.sort(
        key=lambda value: (
            value.constraints.status != "compatible",
            -value.score,
            value.product.sku,
        )
    )
    if min_score is not None and scored and scored[0].score < min_score:
        return []
    return scored
