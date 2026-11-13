from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from kpdesk.config import PROJECT_ROOT
from kpdesk.matching.abstention import (
    calculate_ranked_cases,
    calibrate_threshold,
    evaluate_abstention,
    evidence_row,
)
from kpdesk.matching.artifact import load_trusted_artifact
from kpdesk.matching.cli import load_dataset
from kpdesk.matching.features import FEATURE_SCHEMA
from kpdesk.matching.training import split_groups
from scripts.evaluate_no_match_holdout import _HOLDOUT, load_holdout

_MODELS = PROJECT_ROOT / "ml/models"


def evaluate_candidate(dataset_path: Path, model_path: Path) -> dict[str, Any]:
    dataset = load_dataset(dataset_path)
    if dataset.provenance_kind != "synthetic_example":
        raise ValueError("Only synthetic training data are accepted.")
    model = load_trusted_artifact(model_path, expected_schema=FEATURE_SCHEMA)
    if model.dataset_sha256 != dataset.source_sha256:
        raise ValueError("Model training dataset does not match.")
    split = split_groups(dataset.queries, seed=model.split_seed)
    catalog = list(dataset.products.values())
    validation = calculate_ranked_cases(
        split.validation, catalog, mode="ml", model=model,
    )
    threshold, validation_score = calibrate_threshold(validation)
    internal = evaluate_abstention(
        calculate_ranked_cases(split.test, catalog, mode="ml", model=model),
        threshold,
    )
    frozen = load_holdout(_HOLDOUT, dataset.queries, dataset.products)
    no_match_cases = calculate_ranked_cases(
        frozen, catalog, mode="ml", model=model,
    )
    external = evaluate_abstention(no_match_cases, threshold)
    reasons = {
        item["id"]: item["reason"]
        for item in json.loads(_HOLDOUT.read_text(encoding="utf-8"))["cases"]
    }
    return {
        "artifact_format": "kpdesk-a46-synthetic-candidate-evaluation-v1",
        "training_data_kind": "synthetic_example",
        "production_eligible": False,
        "no_customer_data_used": True,
        "holdout_is_regression_diagnostic": True,
        "used_holdout_for_training_or_calibration": False,
        "source_sha256": dataset.source_sha256,
        "model_sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
        "holdout_sha256": hashlib.sha256(_HOLDOUT.read_bytes()).hexdigest(),
        "split_seed": model.split_seed,
        "train_queries": len(split.train),
        "validation_queries": len(split.validation),
        "test_queries": len(split.test),
        "selected_validation_threshold": threshold,
        "validation": evidence_row(validation_score),
        "internal_test": evidence_row(internal),
        "frozen_no_match_diagnostic": {
            **evidence_row(external),
            "by_reason": {
                reason: {
                    "queries": sum(reasons[c.query_id] == reason for c in no_match_cases),
                    "false_suggestions": sum(
                        reasons[c.query_id] == reason
                        and bool(c.ranked)
                        and c.ranked[0][1] >= threshold
                        for c in no_match_cases
                    ),
                }
                for reason in (
                    "absent_category", "incompatible_profile", "semantic_absence",
                )
            },
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    output = args.report.resolve()
    if (
        output.parent != _MODELS.resolve()
        or output.suffix != ".json"
        or output in (args.dataset.resolve(), args.model.resolve(), _HOLDOUT.resolve())
    ):
        raise ValueError("Report must be created in trusted models directory.")
    report = evaluate_candidate(args.dataset, args.model)
    with args.report.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
