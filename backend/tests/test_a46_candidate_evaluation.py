from __future__ import annotations

from pathlib import Path

import pytest

from kpdesk.config import PROJECT_ROOT
from scripts.evaluate_a46_candidate import evaluate_candidate, main

_SOURCE = PROJECT_ROOT / "ml/data/synthetic/ml/semantic-hard-negatives-a46-20261023.json"
_MODEL = PROJECT_ROOT / "ml/models/semantic-hard-negatives-a46-20261023.joblib"


def test_candidate_is_experiment_only_with_frozen_validation_threshold() -> None:
    report = evaluate_candidate(_SOURCE, _MODEL)
    assert report["training_data_kind"] == "synthetic_example"
    assert report["production_eligible"] is False
    assert report["used_holdout_for_training_or_calibration"] is False
    assert report["holdout_is_regression_diagnostic"] is True
    assert report["train_queries"] + report["validation_queries"] + report["test_queries"] == 233
    internal = report["internal_test"]
    assert (
        internal["positive_top1_correct"]
        + internal["positive_top1_wrong"]
        + internal["positive_abstained"]
        == internal["positive_queries"]
    )
    heldout = report["frozen_no_match_diagnostic"]
    assert heldout["no_match_queries"] == 22
    assert heldout["no_match_correct_empty"] + heldout["no_match_false_suggestion"] == 22


def test_evaluation_rejects_unrelated_dataset_and_unsafe_output(tmp_path: Path) -> None:
    unrelated = PROJECT_ROOT / "ml/data/synthetic/ml/hardcases-20261022.json"
    with pytest.raises(ValueError, match="training dataset"):
        evaluate_candidate(unrelated, _MODEL)
    with pytest.raises(ValueError, match="trusted models directory"):
        main([
            "--dataset", str(_SOURCE), "--model", str(_MODEL),
            "--report", str(tmp_path / "untrusted.json"),
        ])
