from __future__ import annotations

import json
from pathlib import Path

import pytest

from kpdesk.config import PROJECT_ROOT
from kpdesk.matching.cli import load_dataset
from scripts.evaluate_no_match_holdout import (
    _HOLDOUT,
    _SOURCE,
    benchmark_holdout,
    load_holdout,
    main,
)


def test_independent_negative_holdout_is_bounded_and_group_disjoint() -> None:
    training = load_dataset(_SOURCE)
    cases = load_holdout(_HOLDOUT, training.queries, training.products)
    assert len(cases) == 22
    assert len({case.query_id for case in cases}) == 22
    assert all(not case.relevant_skus for case in cases)
    assert {case.query_group_id for case in cases}.isdisjoint({
        query.query_group_id for query in training.queries
    })


@pytest.mark.parametrize("mutation", [
    "duplicate_id",
    "duplicate_training_text",
    "false_absent_category",
    "false_incompatible_profile",
    "false_semantic_exact_name",
    "unsupported_category",
    "profile_overrides_category",
    "wrong_provenance",
])
def test_mislabeled_or_leaking_benchmark_is_rejected(
    tmp_path: Path, mutation: str,
) -> None:
    training = load_dataset(_SOURCE)
    body = json.loads(_HOLDOUT.read_text(encoding="utf-8"))
    cases = body["cases"]
    if mutation == "duplicate_id":
        cases[1]["id"] = cases[0]["id"]
    elif mutation == "duplicate_training_text":
        cases[0]["description"] = training.queries[0].request.description
    elif mutation == "false_absent_category":
        cases[0]["category"] = "paper"
    elif mutation == "false_incompatible_profile":
        cases[6]["profile"] = {}
    elif mutation == "false_semantic_exact_name":
        cases[-1]["description"] = next(
            product.name for product in training.products.values()
            if product.category == "pen"
        )
    elif mutation == "unsupported_category":
        cases[0]["category"] = "unknown"
    elif mutation == "profile_overrides_category":
        cases[0]["profile"] = {"category": "paper"}
    else:
        body["provenance"]["used_for_training"] = True
    target = tmp_path / "invalid.json"
    target.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    with pytest.raises((ValueError, TypeError)):
        load_holdout(target, training.queries, training.products)


def test_report_evaluates_frozen_thresholds_without_promoting_model() -> None:
    report = benchmark_holdout()
    assert report["holdout_queries"] == 22
    assert report["production_eligible"] is False
    assert report["thresholds_recalibrated"] is False
    assert report["independent_customer_labels"] is False
    for mode in ("ml", "tfidf"):
        evaluation = report["comparisons"][mode]
        assert evaluation["no_match_queries"] == 22
        assert (
            evaluation["correctly_abstained"] + evaluation["false_suggestions"]
        ) == 22
        assert sum(
            reason["queries"] for reason in evaluation["by_reason"].values()
        ) == 22
        assert all(
            case["reason"] in (
                "absent_category", "incompatible_profile", "semantic_absence"
            )
            for case in evaluation["cases"]
        )


def test_cli_rejects_model_root_escape(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="local ML models"):
        main(["--output", str(tmp_path / "outside.json")])


def test_existing_benchmark_must_not_be_overwritten() -> None:
    path = PROJECT_ROOT / "ml/models/hardcases-20261022.abstention.json"
    with pytest.raises(FileExistsError):
        main(["--output", str(path)])
