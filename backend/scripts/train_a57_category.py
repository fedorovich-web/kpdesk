from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from kpdesk.config import PROJECT_ROOT
from kpdesk.matching.category_classifier import (
    CategoryClassifier,
    calibrate_category_confidence,
    save_category_model,
    stratified_category_groups,
    train_category_classifier,
)
from kpdesk.matching.cli import load_dataset
from kpdesk.matching.normalize import normalize_text
from kpdesk.matching.profiles import CATEGORY_ATTRIBUTES
from scripts.build_a57_contrastive_training import (
    _SOURCE,
    build_contrastive_training,
)

_MODELS = PROJECT_ROOT / "ml/models"
_SEALED = PROJECT_ROOT / "ml/data/synthetic/ml/a57-sealed-category-holdout-20261023.json"


def train_candidate(training_path: Path, model_path: Path, report_path: Path) -> dict[str, Any]:
    if model_path.exists() or model_path.with_suffix(".joblib.manifest.json").exists():
        raise FileExistsError("Category model already exists.")
    if report_path.exists() or report_path.resolve().parent != _MODELS.resolve():
        raise ValueError("Report path must be new and inside trusted models directory.")
    augmented = load_dataset(training_path)
    if augmented.provenance_kind != "synthetic_example":
        raise ValueError("Training must use synthetic examples only.")
    if json.loads(training_path.read_text(encoding="utf-8")) != build_contrastive_training():
        raise ValueError("Augmented dataset differs from the pinned train-only builder.")
    original = load_dataset(_SOURCE)
    split = stratified_category_groups(original.queries, seed=42)
    old_groups = {record.query_group_id for record in (*split.validation, *split.test)}
    old_texts = {
        normalize_text(record.request.description)
        for record in (*split.validation, *split.test)
    }
    if old_groups & {item.query_group_id for item in augmented.queries}:
        raise ValueError("Previously held-out group entered new training.")
    if old_texts & {
        normalize_text(item.request.description) for item in augmented.queries
    }:
        raise ValueError("Previously held-out request text entered training.")
    if {item.request.category for item in augmented.queries} != set(CATEGORY_ATTRIBUTES):
        raise ValueError("Training source does not cover the complete taxonomy.")
    sealed_bytes = _SEALED.read_bytes()
    sealed = json.loads(sealed_bytes.decode("utf-8"))
    challenge_texts = {
        normalize_text(item["description"])
        for item in [*sealed["cases"], *sealed["unknown"]]
    }
    all_known_texts = {
        normalize_text(item.request.description) for item in (*original.queries, *augmented.queries)
    }
    if (
        sealed["provenance"]["frozen_before_training"] is not True
        or sealed["provenance"]["not_used_for_fit_or_calibration"] is not True
        or len(sealed["cases"]) != len(CATEGORY_ATTRIBUTES)
        or {item["category"] for item in sealed["cases"]} != set(CATEGORY_ATTRIBUTES)
        or len(sealed["unknown"]) != 12
        or len(challenge_texts) != len(sealed["cases"]) + len(sealed["unknown"])
        or challenge_texts & all_known_texts
    ):
        raise ValueError("Sealed synthetic acceptance data overlap or invalid.")

    trained = train_category_classifier(augmented.queries, feature_mode="hybrid")
    threshold, calibration = calibrate_category_confidence(
        trained, split.validation, min_margin=0.03, min_precision=0.95,
    )
    classifier = CategoryClassifier(
        pipeline=trained,
        threshold=threshold,
        min_margin=0.03,
        dataset_sha256=augmented.source_sha256,
        split_seed=42,
    )
    save_category_model(model_path, classifier)
    report = {
        "phase": "a57_train_validation_only",
        "model_sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
        "training_sha256": augmented.source_sha256,
        "baseline_sha256": original.source_sha256,
        "sealed_holdout_sha256": hashlib.sha256(sealed_bytes).hexdigest(),
        "feature_mode": "hybrid",
        "trained_categories": len(CATEGORY_ATTRIBUTES),
        "train_queries": len(augmented.queries),
        "train_groups": len({row.query_group_id for row in augmented.queries}),
        "original_validation_queries": len(split.validation),
        "original_test_queries_excluded": len(split.test),
        "validation_only_calibration": calibration,
        "threshold": threshold,
        "test_was_not_read_for_training": True,
        "holdout_results_unseen": True,
        "production_eligible": False,
    }
    with report_path.open("x", encoding="utf-8") as output:
        json.dump(report, output, sort_keys=True, indent=2, ensure_ascii=False)
        output.write("\n")
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(train_candidate(args.dataset, args.model, args.report), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
