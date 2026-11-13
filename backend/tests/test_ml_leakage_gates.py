"""ML training cannot hide retrieval misses or bypass group isolation."""
from __future__ import annotations

from dataclasses import replace

import pytest
from test_ml_training import labeled_queries, product

from kpdesk.matching.schema import CatalogProduct
from kpdesk.matching.training import (
    DatasetSplit,
    TrainingConfig,
    _pairs_for_training,
    split_groups,
    train_reranker,
)


def test_reviewed_positive_missing_retrieval_must_not_be_silently_dropped() -> None:
    original = labeled_queries()[0]
    request = original.request.model_copy(update={"description": "Бумага A4 80 цветная"})
    case = replace(original, request=request)
    items = [
        product("paper-color", "Бумага A4 80 цветная"),
        product("paper-white", "Бумага A4 80 белая"),
    ]
    with pytest.raises(ValueError, match="positive.*retriev|retriev.*positive"):
        _pairs_for_training(
            [case], items, limit=1,
            explicit_negatives={case.query_id: frozenset({"paper-color"})},
            reviewed_only=True,
        )


def test_train_refuses_manual_split_with_same_group_in_test() -> None:
    queries = labeled_queries()
    known = split_groups(queries, seed=42)
    leaked = DatasetSplit(
        train=known.train, validation=known.validation, test=(*known.test, known.train[0]),
    )
    with pytest.raises(ValueError, match="leak|overlap"):
        train_reranker(
            leaked,
            TrainingConfig(products={
                "paper-white": product("paper-white", "Бумага A4 80 белая"),
                "paper-color": product("paper-color", "Бумага A4 80 цветная"),
            }),
        )


def test_train_refuses_missing_label_sku_before_model_fit() -> None:
    split = split_groups(labeled_queries(), seed=12)
    corrupt = replace(split.train[0], relevant_skus=frozenset({"not-in-catalog"}))
    altered = DatasetSplit(
        train=(corrupt, *split.train[1:]),
        validation=split.validation, test=split.test,
    )
    with pytest.raises(ValueError, match="unknown.*SKU|catalog"):
        train_reranker(
            altered,
            TrainingConfig(products={
                "paper-white": product("paper-white", "Бумага A4 80 белая"),
                "paper-color": product("paper-color", "Бумага A4 80 цветная"),
            }),
        )

def _catalog() -> dict[str, CatalogProduct]:
    return {
        "paper-white": product("paper-white", "Бумага A4 80 белая"),
        "paper-color": product("paper-color", "Бумага A4 80 цветная"),
    }


def test_train_rejects_normalized_text_leakage_in_prebuilt_split() -> None:
    split = split_groups(labeled_queries(), seed=12)
    reused_request = split.train[0].request.model_copy(update={
        "description": "   " + split.validation[0].request.description.upper() + "  ",
    })
    leaked_train = replace(split.train[0], request=reused_request)
    altered = DatasetSplit(
        train=(leaked_train, *split.train[1:]),
        validation=split.validation, test=split.test,
    )
    with pytest.raises(ValueError, match="text leakage|cross.group"):
        train_reranker(altered, TrainingConfig(products=_catalog(), seed=12))


def test_train_rejects_conflicting_reviewed_negative_in_validation() -> None:
    split = split_groups(labeled_queries(), seed=12)
    validation_case = split.validation[0]
    with pytest.raises(ValueError, match="Reviewed negative conflicts"):
        train_reranker(split, TrainingConfig(
            products=_catalog(), seed=12,
            explicit_hard_negatives={
                validation_case.query_id: validation_case.relevant_skus,
            },
        ))


def test_train_rejects_unknown_reviewed_negative_in_test() -> None:
    split = split_groups(labeled_queries(), seed=12)
    test_case = split.test[0]
    with pytest.raises(ValueError, match="unknown SKU"):
        train_reranker(split, TrainingConfig(
            products=_catalog(), seed=12,
            explicit_hard_negatives={
                test_case.query_id: frozenset({"invented-catalog-item"}),
            },
        ))


def test_train_reviewed_only_requires_negative_provenance_in_holdout() -> None:
    split = split_groups(labeled_queries(), seed=12)
    all_queries = (*split.train, *split.validation, *split.test)
    negatives = {
        q.query_id: frozenset(
            {"paper-color" if "paper-white" in q.relevant_skus else "paper-white"}
        )
        for q in all_queries if q.query_id != split.test[0].query_id
    }
    with pytest.raises(ValueError, match="Reviewed negative labels missing"):
        train_reranker(split, TrainingConfig(
            products=_catalog(), seed=12,
            explicit_hard_negatives=negatives, reviewed_only=True,
        ))


def test_train_rejects_incompatible_positive_in_untouched_test_split() -> None:
    split = split_groups(labeled_queries(), seed=12)
    contradictory = replace(
        split.test[0],
        request=split.test[0].request.model_copy(update={"paper_format": "A3"}),
    )
    with pytest.raises(ValueError, match="Gold positive conflicts with hard constraints"):
        train_reranker(
            DatasetSplit(split.train, split.validation, (contradictory, *split.test[1:])),
            TrainingConfig(products=_catalog(), seed=12),
        )


@pytest.mark.parametrize('conflicting_skus', [frozenset(), frozenset({'paper-color'})])
def test_split_rejects_identical_request_with_conflicting_relevance_labels(
    conflicting_skus: frozenset[str],
) -> None:
    cases = labeled_queries()
    same_request = cases[0].request.model_copy(update={
        'description': '  БУМАГА   A4 80 БЕЛАЯ  ',
    })
    conflict = replace(cases[0], query_id='conflicting-label', request=same_request,
                       relevant_skus=conflicting_skus)
    with pytest.raises(ValueError, match='Conflicting relevance labels'):
        split_groups([*cases, conflict], seed=12)


def test_prebuilt_training_split_cannot_bypass_conflicting_label_check() -> None:
    split = split_groups(labeled_queries(), seed=12)
    original = split.train[0]
    conflict = replace(original, query_id='conflicting-label', relevant_skus=frozenset())
    with pytest.raises(ValueError, match='Conflicting relevance labels'):
        train_reranker(
            DatasetSplit((*split.train, conflict), split.validation, split.test),
            TrainingConfig(products=_catalog(), seed=12),
        )


def test_same_text_with_different_structured_requirements_is_not_a_label_conflict() -> None:
    cases = labeled_queries()
    changed_format = replace(
        cases[0], query_id='different-format', relevant_skus=frozenset({'paper-a3'}),
        request=cases[0].request.model_copy(update={'paper_format': 'A3'}),
    )
    split = split_groups([*cases, changed_format], seed=12)
    assert {row.query_id for row in (*split.train, *split.validation, *split.test)} == {
        'q1', 'q2', 'q3', 'q4', 'q5', 'q6', 'q7', 'q8', 'different-format',
    }


def test_same_request_with_consistent_relevance_labels_remains_valid() -> None:
    cases = labeled_queries()
    repeated = replace(cases[0], query_id='consistent-label')
    split = split_groups([*cases, repeated], seed=12)
    assert sum(map(len, (split.train, split.validation, split.test))) == 9
