"""A38: one-time validation-only abstention calibration for an existing trained model.

No model.fit, hyperparameter tuning or threshold selection on the test split.
Runs both trained model and TF-IDF baseline on identical catalog/retrieval.
The test report is diagnostic only; these are synthetic labels.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from kpdesk.matching.abstention import (
    calculate_ranked_cases,
    calibrate_threshold,
    evaluate_abstention,
    evidence_row,
)
from kpdesk.matching.artifact import DEFAULT_TRUSTED_ROOT, load_trusted_artifact
from kpdesk.matching.cli import load_dataset
from kpdesk.matching.features import FEATURE_SCHEMA
from kpdesk.matching.training import split_groups


def calibration_report(dataset: Path, model: Path) -> dict[str, Any]:
    data = load_dataset(dataset)
    artifact = load_trusted_artifact(
        model, expected_schema=FEATURE_SCHEMA, trusted_root=DEFAULT_TRUSTED_ROOT,
    )
    if artifact.dataset_sha256 != data.source_sha256:
        raise ValueError("Training corpus SHA differs from model source.")
    if artifact.training_data_kind != "synthetic_example" or data.provenance_kind != "synthetic_example":
        raise ValueError("A38 is restricted to independent synthetic experiments.")
    split = split_groups(data.queries, seed=artifact.split_seed)
    catalog = list(data.products.values())
    modes: tuple[Literal["ml", "tfidf"], ...] = ("ml", "tfidf")
    scored = {
        mode: {
            "validation": calculate_ranked_cases(
                split.validation, catalog, mode=mode, model=artifact if mode == "ml" else None,
            ),
            "test": calculate_ranked_cases(
                split.test, catalog, mode=mode, model=artifact if mode == "ml" else None,
            ),
        }
        for mode in modes
    }
    reports: dict[str, Any] = {}
    for mode, partitions in scored.items():
        validation = partitions["validation"]
        heldout = partitions["test"]
        threshold, selected = calibrate_threshold(validation)
        reports[mode] = {
            "calibration_partition": "validation",
            "threshold": threshold,
            "validation": evidence_row(selected),
            "test_diagnostic": evidence_row(evaluate_abstention(heldout, threshold)),
            "test_without_abstention": evidence_row(evaluate_abstention(heldout, 0.0)),
        }
    return {
        "artifact_format": "synthetic-ml-abstention-report-v1",
        "dataset_sha256": data.source_sha256,
        "model_sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
        "feature_schema": FEATURE_SCHEMA,
        "split_seed": artifact.split_seed,
        "training_data_kind": "synthetic_example",
        "train_queries": len(split.train),
        "validation_queries": len(split.validation),
        "test_queries": len(split.test),
        "validation_no_match": sum(not query.relevant_skus for query in split.validation),
        "test_no_match": sum(not query.relevant_skus for query in split.test),
        "test_used_for_threshold_selection": False,
        "production_benchmark": False,
        "production_eligible": False,
        "models": reports,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Calibration and abstention diagnostics (synthetic).")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.resolve() in (args.dataset.resolve(), args.model.resolve()):
        raise ValueError("Report output may not overwrite model or training data.")
    report = calibration_report(args.dataset, args.model)
    with args.output.open("x", encoding="utf-8") as destination:
        json.dump(report, destination, ensure_ascii=False, sort_keys=True, indent=2)
        destination.write("\n")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
