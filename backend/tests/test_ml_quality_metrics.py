"""Explicit retrieval/quality indicators: no-match, reciprocal rank, nDCG and groups."""

import math
from decimal import Decimal

import pytest
from test_ml_training import labeled_queries, product

from kpdesk.matching.detailed_metrics import (
    compare_lexical_baselines,
    detailed_ranking_metrics,
)
from kpdesk.matching.features import (
    FEATURE_SCHEMA,
    PairFeatureExtractor,
    PairInput,
    build_pair_features,
)
from kpdesk.matching.schema import CatalogProduct, EvaluationCase, RequestedItem
from kpdesk.matching.training import split_groups


def test_mrr_ndcg_and_no_match_use_positive_denominator_and_rank_order() -> None:
    cases = [
        EvaluationCase("q1", frozenset(("A", "C"))),
        EvaluationCase("q2", frozenset(("D",))),
        EvaluationCase("q3", frozenset()),
    ]
    ranked = {
        "q1": ["X", "A", "C"],
        "q2": ["X", "Z", "D"],
        "q3": ["X", "Y"],
    }
    result = detailed_ranking_metrics(cases, ranked, k=3)
    assert result.positive_queries == 2
    assert result.no_match_queries == 1
    assert result.hit_at_k == 2
    assert result.hit_at_1 == 0
    assert result.false_suggestions_on_no_match == 1
    assert result.correct_empty_no_match == 0
    assert result.mrr_at_k == pytest.approx((1 / 2 + 1 / 3) / 2)
    q1_dcg = (1 / math.log2(3)) + (1 / math.log2(4))
    q1_ideal = 1 + 1 / math.log2(3)
    q2_dcg = 1 / math.log2(4)
    assert result.ndcg_at_k == pytest.approx((q1_dcg / q1_ideal + q2_dcg) / 2)


def test_ranking_metrics_no_gold_or_duplicate_predictions() -> None:
    result = detailed_ranking_metrics([EvaluationCase("none", frozenset())], {"none": []}, k=3)
    assert result.mrr_at_k == 0
    assert result.ndcg_at_k == 0
    assert result.correct_empty_no_match == 1
    with pytest.raises(ValueError, match="Duplicate"):
        detailed_ranking_metrics(
            [EvaluationCase("has", frozenset(("A",)))], {"has": ["A", "A"]}, k=3
        )


def test_profile_features_have_specific_schema_and_fixed_feature_order() -> None:
    req = RequestedItem(
        description="Мышь беспроводная 1600 DPI", category="mouse",
        attributes={"connection": "wireless", "dpi": 1600},
    )
    a = CatalogProduct(
        sku="M", name="Mouse wireless", category="mouse",
        sale_unit="штука", pieces_per_sale_unit=1, price_rub=Decimal(100),
        attributes={"connection": "wireless", "dpi": 1600},
    )
    b = a.model_copy(update={"price_rub": Decimal(1000)})
    assert FEATURE_SCHEMA == "kpdesk-pair-v3"
    fields = build_pair_features(req, a)
    assert fields.profile_exact_match_fraction == 1.0
    assert fields.request_profile_coverage == 1.0
    assert build_pair_features(req, b) == fields
    extract = PairFeatureExtractor().fit([PairInput(req, a)])
    assert extract.transform([PairInput(req, a)]).shape == (1, 14)


def test_compare_baselines_on_same_queries_without_gold_injection() -> None:
    queries = labeled_queries()
    products = {
        "paper-white": product("paper-white", "Бумага A4 80 белая"),
        "paper-color": product("paper-color", "Бумага A4 80 цветная"),
    }
    split = split_groups(queries, seed=12)
    evaluated = compare_lexical_baselines(split.validation, products, max_candidates=20)
    assert set(evaluated) == {"rapidfuzz", "tfidf"}
    assert all(item.positive_queries == len(split.validation) for item in evaluated.values())
    assert all(0 <= item.mrr_at_k <= 1 for item in evaluated.values())
