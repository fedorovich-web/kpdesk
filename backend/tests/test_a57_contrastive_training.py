from __future__ import annotations

import json
from pathlib import Path

import pytest

from kpdesk.config import PROJECT_ROOT
from kpdesk.matching.category_classifier import stratified_category_groups
from kpdesk.matching.cli import load_dataset
from kpdesk.matching.normalize import normalize_text
from kpdesk.matching.profiles import CATEGORY_ATTRIBUTES
from scripts import train_a57_category
from scripts.build_a57_contrastive_training import (
    _SOURCE,
    build_contrastive_training,
    main,
)
from scripts.evaluate_a57_category import promotion_checks, read_frozen_cases


def test_a57_corpus_is_complete_synthetic_and_previous_test_is_excluded(
    tmp_path: Path,
) -> None:
    path = tmp_path / "train.json"
    assert main(["--output", str(path)]) == 0
    corpus = load_dataset(path)
    original = load_dataset(_SOURCE)
    split = stratified_category_groups(original.queries)
    originals = {row.query_id for row in original.queries}
    old_train = {row.query_id for row in split.train}
    old_validation = {row.query_id for row in split.validation}
    old_test = {row.query_id for row in split.test}
    current = {row.query_id for row in corpus.queries}
    assert len(corpus.queries) == 303
    assert len(corpus.products) == 213
    assert len({row.query_group_id for row in corpus.queries}) == 133
    assert corpus.provenance_kind == "synthetic_example"
    assert corpus.independent_labels is False
    assert current & originals == old_train
    assert not current & old_validation
    assert not current & old_test
    assert {row.request.category for row in corpus.queries} == set(CATEGORY_ATTRIBUTES)
    assert all(row.relevant_skus for row in corpus.queries)
    assert corpus.products["a57-binder-3"].attributes["rings"] == 4
    assert corpus.products["a57-pencil-27"].attributes["lead_grade"] == "2B"
    assert corpus.products["a57-pencil-28"].attributes["lead_grade"] == "H"
    assert len({normalize_text(row.request.description) for row in corpus.queries}) == len(corpus.queries)
    assert build_contrastive_training()["provenance"]["previous_validation_or_test_in_train"] is False
    second = tmp_path / "second.json"
    assert main(["--output", str(second)]) == 0
    assert load_dataset(second).source_sha256 == corpus.source_sha256
    with pytest.raises(FileExistsError):
        main(["--output", str(path)])


def test_a57_pre_fit_sealed_challenges_have_no_exact_training_overlap() -> None:
    root = PROJECT_ROOT / "ml/data/synthetic/ml"
    corpus = load_dataset(root / "a57-contrastive-verified-20261023.json")
    original = load_dataset(_SOURCE)
    challenge = json.loads((root / "a57-sealed-category-holdout-20261023.json").read_text(
        encoding="utf-8"
    ))
    assert challenge["provenance"]["frozen_before_training"] is True
    assert challenge["provenance"]["not_used_for_fit_or_calibration"] is True
    assert len(challenge["cases"]) == 40
    assert len(challenge["unknown"]) == 12
    assert {case["category"] for case in challenge["cases"]} == set(CATEGORY_ATTRIBUTES)
    corpus_texts = {
        normalize_text(row.request.description) for row in (*corpus.queries, *original.queries)
    }
    holdout_texts = [
        normalize_text(item["description"]) for item in (*challenge["cases"], *challenge["unknown"])
    ]
    assert len(holdout_texts) == len(set(holdout_texts))
    assert not corpus_texts & set(holdout_texts)


def test_a57_training_refuses_modified_corpus_without_fitting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(train_a57_category, "_MODELS", tmp_path)
    payload = build_contrastive_training()
    payload["queries"][0]["request"]["description"] += " посторонний пример"
    path = tmp_path / "modified.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="pinned train-only builder"):
        train_a57_category.train_candidate(
            path, tmp_path / "candidate.joblib", tmp_path / "evaluation.json",
        )
    assert not (tmp_path / "candidate.joblib").exists()


@pytest.mark.parametrize("damage", ("provenance", "category", "duplicate"))
def test_a57_frozen_holdout_rejects_corruption(tmp_path: Path, damage: str) -> None:
    source = PROJECT_ROOT / "ml/data/synthetic/ml/a57-sealed-category-holdout-20261023.json"
    original = json.loads(source.read_text(encoding="utf-8"))
    if damage == "provenance":
        original["provenance"]["frozen_before_training"] = False
    elif damage == "category":
        original["cases"][0]["category"] = "unrecognized"
    else:
        original["unknown"][0]["description"] = original["cases"][0]["description"]
    corrupted = tmp_path / "sealed.json"
    corrupted.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="frozen|Frozen"):
        read_frozen_cases(corrupted)


def test_a57_sealed_holdout_has_expected_independent_challenge_shape() -> None:
    source = PROJECT_ROOT / "ml/data/synthetic/ml/a57-sealed-category-holdout-20261023.json"
    positive, unknown = read_frozen_cases(source)
    assert len(positive) == len(CATEGORY_ATTRIBUTES)
    assert len(unknown) == 12
    assert {label for label, _ in positive} == set(CATEGORY_ATTRIBUTES)


def test_a57_promotion_gate_requires_superiority_and_independent_support() -> None:
    candidate = {
        "correct_accepted": 24,
        "incorrect_accepted": 0,
        "unknown_false_suggestions": [],
    }
    previous = {
        "hybrid_a46": {
            "correct_accepted": 25,
            "incorrect_accepted": 0,
            "unknown_false_suggestions": [],
        },
    }
    counts = dict.fromkeys(CATEGORY_ATTRIBUTES, 1)
    checks = promotion_checks(
        diagnostic_checks={"precision": True},
        candidate=candidate,
        baselines=previous,
        category_counts=counts,
    )
    assert checks["better_positive_hits_than_prior_models"] is False
    assert checks["at_least_three_independent_cases_per_category"] is False
    assert all(checks.values()) is False
    accepted = promotion_checks(
        diagnostic_checks={"precision": True},
        candidate={**candidate, "correct_accepted": 26},
        baselines=previous,
        category_counts=dict.fromkeys(CATEGORY_ATTRIBUTES, 3),
    )
    assert all(accepted.values())
    rejected = promotion_checks(
        diagnostic_checks={"precision": False},
        candidate={**candidate, "correct_accepted": 26},
        baselines=previous,
        category_counts=dict.fromkeys(CATEGORY_ATTRIBUTES, 3),
    )
    assert rejected["diagnostic_checks_passed"] is False
