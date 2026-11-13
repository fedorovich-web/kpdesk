from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from kpdesk.config import PROJECT_ROOT
from kpdesk.matching.category_classifier import (
    CategoryClassifier,
    load_category_model,
    suggest_category_text,
)
from kpdesk.matching.normalize import normalize_text
from kpdesk.matching.profiles import CATEGORY_ATTRIBUTES
from kpdesk.matching.runtime import CATEGORY_NAME
from scripts.evaluate_a46_taxonomy_coverage import category_quality_checks

_ROOT = PROJECT_ROOT / "ml/models"
_HOLDOUT = PROJECT_ROOT / "ml/data/synthetic/ml/a57-sealed-category-holdout-20261023.json"
_VALIDATION = _ROOT / "category-a57-contrastive-verified-20261023.validation.json"
_BASELINES = (
    ("active_a39", CATEGORY_NAME),
    ("char_a46", "category-a46-coverage-20261023.joblib"),
    ("hybrid_a46", "category-a46-hybrid-20261023.joblib"),
)


def read_frozen_cases(
    path: Path,
) -> tuple[list[tuple[str, str]], list[str]]:
    body = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(body, dict) or set(body) != {"provenance", "cases", "unknown"} or body["provenance"] != {
        "kind": "synthetic_example",
        "annotation_method": "a57_prior_fit_sealed_category_challenges",
        "frozen_before_training": True,
        "not_used_for_fit_or_calibration": True,
        "customer_gold": False,
    }:
        raise ValueError("Invalid frozen synthetic challenge provenance.")
    positives = body["cases"]
    negatives = body["unknown"]
    if (
        not isinstance(positives, list)
        or not isinstance(negatives, list)
        or len(positives) != len(CATEGORY_ATTRIBUTES)
        or len(negatives) != 12
        or any(not isinstance(p, dict) or set(p) != {"id", "category", "description"}
               for p in positives)
        or any(not isinstance(n, dict) or set(n) != {"id", "description"}
               for n in negatives)
        or any(not isinstance(p["category"], str) for p in positives)
    ):
        raise ValueError("Invalid frozen category challenge layout.")
    entries = [*positives, *negatives]
    if any(not isinstance(item["id"], str)
           or not isinstance(item["description"], str)
           or not 8 <= len(item["description"]) <= 2000
           or not item["description"].strip()
           for item in entries):
        raise ValueError("Invalid frozen challenge identity or description.")
    if (
        len({item["id"] for item in entries}) != len(entries)
        or len({normalize_text(item["description"]) for item in entries}) != len(entries)
        or {item["category"] for item in positives} != set(CATEGORY_ATTRIBUTES)
    ):
        raise ValueError("Frozen challenges overlap or omit taxonomy categories.")
    return (
        [(item["category"], item["description"]) for item in positives],
        [item["description"] for item in negatives],
    )


def score_frozen_cases(
    model: CategoryClassifier,
    positives: Sequence[tuple[str, str]],
    unknown: Sequence[str],
) -> dict[str, Any]:
    predictions = [
        (category, suggest_category_text(model, phrase))
        for category, phrase in positives
    ]
    accepted = sum(item.predicted is not None for _, item in predictions)
    correct = sum(item.predicted == category for category, item in predictions)
    unknown_predictions = [
        (phrase, suggest_category_text(model, phrase)) for phrase in unknown
    ]
    false_unknown = [
        {"description": phrase, "predicted": item.predicted}
        for phrase, item in unknown_predictions if item.predicted is not None
    ]
    return {
        "positive_queries": len(predictions),
        "accepted": accepted,
        "correct_accepted": correct,
        "incorrect_accepted": accepted - correct,
        "abstained": len(predictions) - accepted,
        "accepted_precision": correct / accepted if accepted else None,
        "coverage": accepted / len(predictions),
        "incorrect_by_category": [
            {"expected": category, "predicted": item.predicted}
            for category, item in predictions
            if item.predicted is not None and item.predicted != category
        ],
        "unknown_queries": len(unknown_predictions),
        "unknown_rejected": len(unknown_predictions) - len(false_unknown),
        "unknown_false_suggestions": false_unknown,
    }


def promotion_checks(
    *,
    diagnostic_checks: Mapping[str, bool],
    candidate: Mapping[str, Any],
    baselines: Mapping[str, Mapping[str, Any]],
    category_counts: Mapping[str, int],
) -> dict[str, bool]:
    return {
        "diagnostic_checks_passed": all(diagnostic_checks.values()),
        "better_positive_hits_than_prior_models": (
            candidate["correct_accepted"] > max(
                prior["correct_accepted"] for prior in baselines.values()
            )
        ),
        "no_additional_false_suggestions": (
            candidate["incorrect_accepted"] <= min(
                prior["incorrect_accepted"] for prior in baselines.values()
            )
            and len(candidate["unknown_false_suggestions"]) <= min(
                len(prior["unknown_false_suggestions"]) for prior in baselines.values()
            )
        ),
        "at_least_three_independent_cases_per_category": (
            set(category_counts) == set(CATEGORY_ATTRIBUTES)
            and min(category_counts.values()) >= 3
        ),
    }


def evaluate_frozen_candidate(model_path: Path) -> dict[str, Any]:
    validation = json.loads(_VALIDATION.read_text(encoding="utf-8"))
    if (
        validation.get("phase") != "a57_train_validation_only"
        or validation.get("holdout_results_unseen") is not True
        or validation.get("production_eligible") is not False
        or validation.get("model_sha256") != hashlib.sha256(model_path.read_bytes()).hexdigest()
        or validation.get("sealed_holdout_sha256") != hashlib.sha256(_HOLDOUT.read_bytes()).hexdigest()
    ):
        raise ValueError("Frozen evaluation or trained model integrity mismatch.")
    positives, unknown = read_frozen_cases(_HOLDOUT)
    candidate = load_category_model(model_path)
    if candidate.dataset_sha256 != validation.get("training_sha256"):
        raise ValueError("Frozen classifier does not match training provenance.")
    scores = score_frozen_cases(candidate, positives, unknown)
    checks = category_quality_checks(
        trained_categories=set(map(str, candidate.pipeline.steps[-1][1].classes_)),
        accepted_precision=scores["accepted_precision"],
        accepted_group_precision=scores["accepted_precision"],
        coverage=scores["coverage"],
        group_coverage=scores["coverage"],
        unknown_false_suggestions=len(scores["unknown_false_suggestions"]),
    )
    baselines = {}
    for name, filename in _BASELINES:
        baseline = _ROOT / filename
        baselines[name] = {
            "model_sha256": hashlib.sha256(baseline.read_bytes()).hexdigest(),
            **score_frozen_cases(load_category_model(baseline), positives, unknown),
        }
    readiness = promotion_checks(
        diagnostic_checks=checks,
        candidate=scores,
        baselines=baselines,
        category_counts=Counter(label for label, _ in positives),
    )
    return {
        "artifact_format": "kpdesk-a57-prospective-synthetic-diagnostic-v1",
        "holdout_sha256": validation["sealed_holdout_sha256"],
        "model_sha256": validation["model_sha256"],
        "training_sha256": validation["training_sha256"],
        "frozen_before_candidate_fit": True,
        "not_used_for_threshold_calibration": True,
        "synthetic_only": True,
        "single_case_per_taxonomy_category": True,
        "small_holdout_not_production_statistical_evidence": True,
        "additional_independent_acceptance_required": True,
        "production_eligible": False,
        "quality_checks": checks,
        "quality_gate_passed": all(checks.values()),
        "promotion_checks": readiness,
        "promotion_gate_passed": all(readiness.values()),
        "candidate": scores,
        "baselines": baselines,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    if (
        args.model.resolve().parent != _ROOT.resolve()
        or args.report.resolve().parent != _ROOT.resolve()
        or args.report.suffix != ".json"
        or args.report.exists()
    ):
        raise ValueError("Evaluation inputs must use trusted paths and a new report.")
    result = evaluate_frozen_candidate(args.model)
    with args.report.open("x", encoding="utf-8") as out:
        json.dump(result, out, ensure_ascii=False, sort_keys=True, indent=2)
        out.write("\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
