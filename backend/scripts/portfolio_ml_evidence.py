"""Portfolio evidence pack: verify saved ML artifacts; never replay a sealed holdout.

This is an audit of existing *synthetic* diagnostics, not a new accuracy
measurement, legal provenance approval, or an LLM benchmark.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MODEL_PREFIX = "category-a57-contrastive-verified-20261023"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Portfolio evidence file is missing or too large.")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("Expected a JSON object.")
    return value


def _verify(expected: object, path: Path) -> str:
    actual = sha256(path)
    if expected != actual:
        raise ValueError(f"Evidence hash mismatch for {path.name}")
    return actual


def _metrics(value: Mapping[str, Any]) -> dict[str, float | int]:
    accepted = value["accepted"]
    rejected = value["abstained"]
    correct = value["correct_accepted"]
    incorrect = value["incorrect_accepted"]
    positive = value["positive_queries"]
    unknown = value["unknown_queries"]
    unknown_rejected = value["unknown_rejected"]
    if any(type(item) is not int or item < 0 for item in (
        accepted, rejected, correct, incorrect, positive, unknown, unknown_rejected,
    )):
        raise ValueError("Malformed stored ML counters")
    if (
        accepted + rejected != positive or correct + incorrect != accepted
        or unknown_rejected > unknown
        or not 0 <= value["accepted_precision"] <= 1
    ):
        raise ValueError("Inconsistent stored ML counters")
    precision = correct / accepted if accepted else 0
    if abs(precision - value["accepted_precision"]) > 1e-9:
        raise ValueError("Invalid stored accepted precision")
    return {
        "positive_queries": positive,
        "accepted": accepted,
        "correct_accepted": correct,
        "unsafe_accepted": incorrect,
        "abstained": rejected,
        "coverage": round(accepted / positive, 6) if positive else 0,
        "accepted_precision": round(precision, 6) if accepted else 0,
        "unknown_queries": unknown,
        "unknown_rejected": unknown_rejected,
    }


def build_evidence(root: Path) -> dict[str, object]:
    models = root / "ml/models"
    synthetic = root / "ml/data/synthetic/ml"
    report_path = models / f"{MODEL_PREFIX}.frozen-test.json"
    manifest_path = models / f"{MODEL_PREFIX}.joblib.manifest.json"
    report, manifest = _load(report_path), _load(manifest_path)
    model_path = models / f"{MODEL_PREFIX}.joblib"
    dataset_path = synthetic / "a57-contrastive-verified-20261023.json"
    holdout_path = synthetic / "a57-sealed-category-holdout-20261023.json"
    if report.get("production_eligible") is not False or (
        manifest.get("production_eligible") is not False
        or manifest.get("training_data_kind") != "synthetic_example"
        or report.get("synthetic_only") is not True
    ):
        raise ValueError("Training provenance/status drift")
    model_sha = _verify(manifest.get("model_sha256"), model_path)
    if model_sha != report.get("model_sha256"):
        raise ValueError("Model SHA differs from sealed diagnostic")
    dataset_sha = _verify(manifest.get("dataset_sha256"), dataset_path)
    if dataset_sha != report.get("training_sha256"):
        raise ValueError("Dataset SHA differs from sealed diagnostic")
    holdout_sha = _verify(report.get("holdout_sha256"), holdout_path)
    summaries = {
        name: _metrics(entry)
        for name, entry in {
            **report["baselines"], "a57_candidate": report["candidate"],
        }.items()
    }
    best = max(
        summaries, key=lambda name: (
            summaries[name]["correct_accepted"],
            -summaries[name]["unsafe_accepted"],
        ),
    )
    return {
        "kind": "verified_existing_synthetic_ml_diagnostics",
        "production_eligible": False,
        "performance_on_human_labeled_real_requests": None,
        "llm_heldout_metrics": None,
        "llm_provider_cost_measured": None,
        "catalog_match_recall_at_5": None,
        "catalog_match_top1": None,
        "model_sha256": model_sha,
        "training_dataset_sha256": dataset_sha,
        "sealed_holdout_sha256": holdout_sha,
        "existing_holdout_reused_for_optimization": False,
        "source_report_sha256": sha256(report_path),
        "provenance": "author-written synthetic-only class diagnostic, 40 positives + 12 unknown",
        "series": summaries,
        "best_by_correct_accepted_not_production_approval": best,
        "a57_proposed_for_runtime": False,
        "blockers": [
            "lawful independent human-labeled request/SKU pairs and explicit negatives",
            "supplier- and request-group-separated blind holdout with enough cases per category",
            "LLM extraction gold, provider cost and p95 latency on authorized data",
            "matching Recall@5 and top-1 on shared real SKU gold",
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("Refusing to overwrite an existing evidence report")
    report = build_evidence(ROOT)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as out:
        json.dump(report, out, ensure_ascii=False, indent=2, sort_keys=True)
        out.write("\n")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
