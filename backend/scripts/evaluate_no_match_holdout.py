from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kpdesk.config import PROJECT_ROOT
from kpdesk.matching.abstention import calculate_ranked_cases, evaluate_abstention
from kpdesk.matching.artifact import load_trusted_artifact
from kpdesk.matching.cli import load_dataset
from kpdesk.matching.constraints import check_constraints
from kpdesk.matching.features import FEATURE_SCHEMA
from kpdesk.matching.normalize import normalize_text
from kpdesk.matching.profiles import CATEGORY_ATTRIBUTES
from kpdesk.matching.schema import CatalogProduct, RequestedItem
from kpdesk.matching.training import LabeledQuery

_SOURCE = PROJECT_ROOT / "ml/data/synthetic/ml/hardcases-20261022.json"
_MODEL = PROJECT_ROOT / "ml/models/hardcases-20261022.joblib"
_CALIBRATION = PROJECT_ROOT / "ml/models/hardcases-20261022.abstention.json"
_HOLDOUT = PROJECT_ROOT / "ml/data/synthetic/ml/no-match-holdout-a46.json"
_KIND = Literal["absent_category", "incompatible_profile", "semantic_absence"]
_IDENT = re.compile(r"a46-[a-z0-9-]{3,80}\Z")


class HoldoutCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str
    reason: _KIND
    category: str
    description: str = Field(min_length=8, max_length=2000)
    profile: dict[str, Any] = Field(default_factory=dict)


class HoldoutProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["synthetic_example"]
    annotation_method: Literal["independent_hand_authored_absent_sku_challenges_20261023"]
    used_for_training: Literal[False]
    used_for_calibration: Literal[False]
    independent_customer_labels: Literal[False]


class HoldoutDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    provenance: HoldoutProvenance
    cases: list[HoldoutCase] = Field(min_length=12, max_length=100)


def load_holdout(
    path: Path, train: Sequence[LabeledQuery], catalog: Mapping[str, CatalogProduct],
) -> tuple[LabeledQuery, ...]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 128_000:
        raise ValueError("Holdout must be an existing bounded regular file.")
    document = HoldoutDocument.model_validate_json(path.read_bytes())
    training_texts = {normalize_text(q.request.description) for q in train}
    training_groups = {q.query_group_id for q in train}
    ids: set[str] = set()
    texts: set[str] = set()
    records: list[LabeledQuery] = []
    reasons: Counter[str] = Counter()
    for case in document.cases:
        if (
            _IDENT.fullmatch(case.id) is None
            or case.id in ids
            or case.id in training_groups
            or case.category not in CATEGORY_ATTRIBUTES
        ):
            raise ValueError("Holdout identity or category is invalid.")
        ids.add(case.id)
        normalized = normalize_text(case.description)
        if normalized in texts or normalized in training_texts:
            raise ValueError("Holdout contains duplicated or training query text.")
        texts.add(normalized)
        if set(case.profile) - {
            "brand", "paper_format", "paper_gsm",
            "pen_ink_color", "pen_ink_type", "attributes",
        }:
            raise ValueError("Holdout profile cannot override request identity or quantities.")
        request = RequestedItem.model_validate({
            "description": case.description, "category": case.category,
            "quantity": 1, "unit": "штука", **case.profile,
        })
        same_category = [
            product for product in catalog.values()
            if product.category == case.category
        ]
        if case.reason == "absent_category" and same_category:
            raise ValueError("Absent-category label conflicts with the catalog.")
        if case.reason == "incompatible_profile" and (
            not same_category or any(
                check_constraints(request, item).status != "incompatible"
                for item in same_category
            )
        ):
            raise ValueError("Incompatible-profile label lacks deterministic evidence.")
        if case.reason == "semantic_absence" and (
            not same_category or any(
                normalize_text(item.name) == normalized for item in same_category
            )
        ):
            raise ValueError("Semantic negative label conflicts with an exact catalog name.")
        records.append(LabeledQuery(
            query_id=case.id, query_group_id=case.id,
            request=request, relevant_skus=frozenset(),
        ))
        reasons[case.reason] += 1
    if set(reasons) != {
        "absent_category", "incompatible_profile", "semantic_absence",
    }:
        raise ValueError("Holdout must include every negative challenge family.")
    return tuple(records)


def benchmark_holdout() -> dict[str, Any]:
    training = load_dataset(_SOURCE)
    if training.provenance_kind != "synthetic_example":
        raise ValueError("Only synthetic training provenance is permitted.")
    artifact = load_trusted_artifact(_MODEL, expected_schema=FEATURE_SCHEMA)
    if artifact.dataset_sha256 != training.source_sha256:
        raise ValueError("Training dataset and model SHA256 differ.")
    calibration = json.loads(_CALIBRATION.read_text(encoding="utf-8"))
    model_hash = hashlib.sha256(_MODEL.read_bytes()).hexdigest()
    if not isinstance(calibration, dict) or (
        calibration.get("dataset_sha256") != training.source_sha256
        or calibration.get("model_sha256") != model_hash
        or calibration.get("test_used_for_threshold_selection") is not False
        or calibration.get("production_eligible") is not False
    ):
        raise ValueError("Frozen calibration does not match this model and corpus.")
    records = load_holdout(_HOLDOUT, training.queries, training.products)
    by_id = {
        case.id: case for case in HoldoutDocument.model_validate_json(
            _HOLDOUT.read_bytes()
        ).cases
    }
    output: dict[str, dict[str, Any]] = {}
    for mode in ("ml", "tfidf"):
        mode_record = calibration["models"][mode]
        threshold = mode_record["threshold"]
        if type(threshold) is not float or not math.isfinite(threshold) or (
            not 0 <= threshold <= 1
        ):
            raise ValueError("Calibration threshold must be a frozen finite float.")
        scored = calculate_ranked_cases(
            records, list(training.products.values()), mode=mode,
            model=artifact if mode == "ml" else None,
        )
        metrics = evaluate_abstention(scored, threshold)
        if metrics.positive_queries != 0 or metrics.no_match_queries != len(records):
            raise ValueError("No-match benchmark must contain only absent-SKU queries.")
        output[mode] = {
            "frozen_validation_threshold": threshold,
            "no_match_queries": metrics.no_match_queries,
            "correctly_abstained": metrics.no_match_correct_empty,
            "false_suggestions": metrics.no_match_false_suggestion,
            "false_suggestion_rate": metrics.no_match_false_suggestion / len(records),
            "by_reason": {
                reason: {
                    "queries": sum(by_id[case.query_id].reason == reason for case in scored),
                    "false_suggestions": sum(
                        by_id[case.query_id].reason == reason
                        and bool(case.ranked)
                        and case.ranked[0][1] >= threshold
                        for case in scored
                    ),
                }
                for reason in ("absent_category", "incompatible_profile", "semantic_absence")
            },
            "cases": [
                {
                    "id": case.query_id,
                    "reason": by_id[case.query_id].reason,
                    "candidate_count": len(case.ranked),
                    "suggested_sku": (
                        case.ranked[0][0] if case.ranked
                        and case.ranked[0][1] >= threshold else None
                    ),
                    "top_score": (
                        round(case.ranked[0][1], 6) if case.ranked else None
                    ),
                }
                for case in scored
            ],
        }
    return {
        "kind": "independent_synthetic_no_match_diagnostic",
        "holdout_sha256": hashlib.sha256(_HOLDOUT.read_bytes()).hexdigest(),
        "model_sha256": model_hash,
        "calibration_dataset_sha256": training.source_sha256,
        "holdout_queries": len(records),
        "production_eligible": False,
        "independent_customer_labels": False,
        "thresholds_recalibrated": False,
        "comparisons": output,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if output.parent != (PROJECT_ROOT / "ml/models").resolve() or output.suffix != ".json":
        raise ValueError("Benchmark output must be a JSON file in local ML models.")
    if args.output.exists():
        raise FileExistsError("Refusing to replace an existing ML benchmark.")
    report = benchmark_holdout()
    with args.output.open("x", encoding="utf-8") as destination:
        json.dump(report, destination, ensure_ascii=False, sort_keys=True, indent=2)
        destination.write("\n")
    print(json.dumps({
        mode: {
            key: value for key, value in scores.items()
            if key != "cases"
        }
        for mode, scores in report["comparisons"].items()
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
