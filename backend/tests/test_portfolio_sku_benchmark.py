"""Synthetic SKU benchmark invariants: blind split, ranking and safe refusal."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.benchmark_portfolio_sku import (
    FIXTURE,
    Prediction,
    _calibrate,
    _load_dataset,
    _score,
    _synthetic_catalog,
    run_benchmark,
)


def _alter(tmp_path: Path, edit: object) -> Path:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert callable(edit)
    edit(data)
    target = tmp_path / "tampered.json"
    target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return target


def test_synthetic_sku_baselines_train_fit_and_score_true_holdout() -> None:
    report = run_benchmark()
    assert report["train_cases"] == 16
    assert report["validation_cases"] == 7
    assert report["test_cases"] == 12
    assert report["training_pair_count"] > 16
    assert report["catalog_sku_count"] == 15
    assert report["test_gold_is_independent_human_reviewed"] is False
    assert report["source_disjoint_evaluation"] is False
    assert report["publishable_real_quality_claim"] is False
    assert report["models"]["llm_assisted"]["status"] == "not_run"
    assert report["models"]["llm_assisted"]["test"] is None
    for mode in ("rapidfuzz", "tfidf", "ml"):
        measured = report["models"][mode]
        test = measured["test"]
        assert measured["threshold_selected_on"] == "validation_only"
        assert test["positive_queries"] == 5
        assert test["no_match_queries"] == 7
        assert test["recall_at_5"] >= test["top_1_accuracy"]
        assert (
            test["no_match_queries_with_empty_candidates"]
            + test["no_match_queries_with_nonempty_candidates"]
            == test["no_match_queries"]
        )
        assert test["positive_automatic_coverage"] <= 1
        assert test["accepted"] == test["correct_accepted"] + test["false_automatic_matches"]
        assert test["cost_rub"] is None
        assert test["false_accept_rate_among_accepted"] is None or (
            0 <= test["false_accept_rate_among_accepted"] <= 1
        )
        assert 0 <= test["coverage_all_queries"] <= 1


def test_group_overlap_rejected_without_training(tmp_path: Path) -> None:
    def change(data: dict[str, object]) -> None:
        cases = data["cases"]
        assert isinstance(cases, list)
        cases[-1]["query_group_id"] = "tr-01"
    path = _alter(tmp_path, change)
    with pytest.raises(ValueError, match="Leaked query group"):
        _load_dataset(path, _synthetic_catalog())


def test_duplicate_query_text_rejected_even_across_groups(tmp_path: Path) -> None:
    def change(data: dict[str, object]) -> None:
        cases = data["cases"]
        assert isinstance(cases, list)
        cases[-1]["request"]["description"] = cases[0]["request"]["description"]
    path = _alter(tmp_path, change)
    with pytest.raises(ValueError, match="Leaked identical request text"):
        _load_dataset(path, _synthetic_catalog())


def test_unknown_gold_sku_is_rejected(tmp_path: Path) -> None:
    def change(data: dict[str, object]) -> None:
        cases = data["cases"]
        assert isinstance(cases, list)
        cases[0]["relevant_skus"] = ["INJECTED-GOLD-NOT-IN-CATALOG"]
    path = _alter(tmp_path, change)
    with pytest.raises(ValueError, match="Gold SKU missing"):
        _load_dataset(path, _synthetic_catalog())


def test_incompatible_gold_is_rejected(tmp_path: Path) -> None:
    def change(data: dict[str, object]) -> None:
        cases = data["cases"]
        assert isinstance(cases, list)
        cases[0]["relevant_skus"] = ["PAPER-A4-65"]
    path = _alter(tmp_path, change)
    with pytest.raises(ValueError, match="mandatory constraints"):
        _load_dataset(path, _synthetic_catalog())


def test_missing_positive_or_negative_split_is_rejected(tmp_path: Path) -> None:
    def change(data: dict[str, object]) -> None:
        cases = data["cases"]
        assert isinstance(cases, list)
        for case in cases:
            if case["split"] == "validation":
                case["relevant_skus"] = []
    path = _alter(tmp_path, change)
    with pytest.raises(ValueError, match="split coverage"):
        _load_dataset(path, _synthetic_catalog())


def test_calibration_must_not_accept_known_wrong_validation_prediction() -> None:
    predictions = [
        Prediction("v-positive", frozenset({"SKU-A"}), ("SKU-X",),
                   "SKU-X", 0.99, "compatible", 0.1),
        Prediction("v-unknown", frozenset(), ("SKU-X",),
                   "SKU-X", 0.97, "compatible", 0.1),
    ]
    threshold = _calibrate(predictions)
    assert threshold > 0.99


def test_metric_denominators_and_false_accepts_are_explicit() -> None:
    catalog = _synthetic_catalog()
    split = _load_dataset(FIXTURE, catalog)
    queries = (split.test[0], split.test[5])
    wrong = Prediction(
        queries[0].query_id, queries[0].relevant_skus,
        ("PAPER-A4-65",), "PAPER-A4-65", .95, "compatible", 5,
    )
    false_no_match = Prediction(
        queries[1].query_id, queries[1].relevant_skus,
        ("PAPER-A4-80",), "PAPER-A4-80", .92, "compatible", 10,
    )
    measured = _score(queries, (wrong, false_no_match), threshold=.9)
    assert measured["positive_queries"] == 1
    assert measured["no_match_queries"] == 1
    assert measured["accepted"] == 2
    assert measured["false_automatic_matches"] == 2
    assert measured["false_accepts_on_no_match"] == 1
    assert measured["top_1_accuracy"] == 0
    assert measured["coverage_all_queries"] == 1
    assert measured["accepted_precision"] == 0
    assert measured["false_accept_rate_among_accepted"] == 1
    assert measured["no_match_rejection_rate"] == 0


def test_ambiguous_top_match_is_never_auto_accepted() -> None:
    predicted = Prediction(
        "pending", frozenset(), ("SKU",), None, None,
        None, 0.1,
    )
    threshold = _calibrate((
        Prediction("positive", frozenset({"SKU"}), ("SKU",),
                   "SKU", 0.9, "compatible", 0.2),
        predicted,
    ))
    assert threshold <= 0.9
