"""A39 train/evaluate request-category classifier, with held-out source groups.

Only the train/validation sets influence weights and confidence. The test
split is evaluated by a separate command after the model is immutable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from kpdesk.matching.category_classifier import (
    CategoryClassifier,
    calibrate_category_confidence,
    load_category_model,
    save_category_model,
    stratified_category_groups,
    suggest_category,
    train_category_classifier,
)
from kpdesk.matching.cli import load_dataset


def _diagnostics(
    model: CategoryClassifier, records: Sequence[Any],
) -> dict[str, Any]:
    total = accepted = correct = rejected = accessory_total = accessory_correct = 0
    wrong: list[dict[str, object]] = []
    for record in records:
        report = suggest_category(model, record.request)
        truth = record.request.category
        total += 1
        if truth in {
            "mouse_pad", "pen_refill", "usb_adapter",
            "notebook_cover", "staple_remover", "marker_refill",
        }:
            accessory_total += 1
            accessory_correct += int(report.predicted == truth)
        if report.predicted is None:
            rejected += 1
            continue
        accepted += 1
        correct += int(report.predicted == truth)
        if report.predicted != truth:
            wrong.append({
                "query_id": record.query_id,
                "expected_category": truth,
                "predicted": report.predicted,
                "confidence": round(report.confidence, 5),
            })
    return {
        "queries": total,
        "accepted": accepted,
        "correct_accepted": correct,
        "incorrect_accepted": accepted - correct,
        "abstained": rejected,
        "coverage": accepted / total if total else 0.0,
        "accepted_precision": correct / accepted if accepted else None,
        "accessory_queries": accessory_total,
        "accessory_correct_accepted": accessory_correct,
        "false_category_overrides": wrong,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline synthetic SKU-category classifier.")
    action = parser.add_subparsers(dest="action", required=True)
    for subcommand in ("train", "evaluate"):
        branch = action.add_parser(subcommand)
        branch.add_argument("--dataset", type=Path, required=True)
        branch.add_argument("--model", type=Path, required=True)
        branch.add_argument("--report", type=Path, required=True)
        if subcommand == "train":
            branch.add_argument("--regularization-c", type=float, default=1.0)
            branch.add_argument("--feature-mode", choices=("char", "hybrid"), default="char")
    args = parser.parse_args(argv)
    data = load_dataset(args.dataset)
    if data.provenance_kind != "synthetic_example":
        raise ValueError("This experiment is restricted to own synthetic data.")
    if args.action == "train":
        split = stratified_category_groups(data.queries, 42)
        classifier = train_category_classifier(
            split.train, seed=42, regularization_c=args.regularization_c,
            feature_mode=args.feature_mode,
        )
        threshold, calibration = calibrate_category_confidence(
            classifier, split.validation,
        )
        fitted = CategoryClassifier(
            pipeline=classifier, threshold=threshold, min_margin=0.03,
            dataset_sha256=data.source_sha256, split_seed=42,
        )
        save_category_model(args.model, fitted)
        result = {
            "phase": "train_and_validation",
            "classifier_regularization_c": args.regularization_c,
            "feature_mode": args.feature_mode,
            "model_sha256": hashlib.sha256(args.model.read_bytes()).hexdigest(),
            "dataset_sha256": data.source_sha256,
            "train_queries": len(split.train),
            "validation_queries": len(split.validation),
            "test_queries_untouched": len(split.test),
            "categories": len({row.request.category for row in data.queries}),
            "confidence_threshold": threshold,
            "validation_calibration": calibration,
            "validation_diagnostics": _diagnostics(fitted, split.validation),
            "test_used_for_fit_or_threshold": False,
            "production_eligible": False,
        }
    else:
        fitted = load_category_model(args.model)
        if fitted.dataset_sha256 != data.source_sha256:
            raise ValueError("Dataset SHA differs from verified model training source.")
        split = stratified_category_groups(data.queries, fitted.split_seed)
        result = {
            "phase": "frozen_model_test_diagnostic",
            "dataset_sha256": data.source_sha256,
            "model_sha256": hashlib.sha256(args.model.read_bytes()).hexdigest(),
            "test_queries": len(split.test),
            "test": _diagnostics(fitted, split.test),
            "threshold_selected_on": "validation",
            "test_used_for_fit_or_threshold": False,
            "production_eligible": False,
        }
    if args.report.resolve() in (args.dataset.resolve(), args.model.resolve()):
        raise ValueError("Report may not overwrite dataset or classifier.")
    with args.report.open("x", encoding="utf-8") as output:
        json.dump(result, output, ensure_ascii=False, sort_keys=True, indent=2)
        output.write("\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
