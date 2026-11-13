"""Offline multi-category retrieval diagnostic; never a production ML benchmark.

Uses synthetically derived SKU distractors, the frozen trusted pairwise model
and strict pre-existing product constraints. No training or external services.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from kpdesk.config import PROJECT_ROOT
from kpdesk.matching.artifact import TrustedModel, load_trusted_artifact
from kpdesk.matching.constraints import check_constraints
from kpdesk.matching.features import FEATURE_SCHEMA
from kpdesk.matching.profiles import CATEGORY_ATTRIBUTES
from kpdesk.matching.rank import rank_candidates
from kpdesk.matching.retrieve import retrieve_candidates
from kpdesk.matching.schema import CatalogProduct, RequestedItem
from scripts.build_a46_taxonomy_coverage import build_taxonomy_coverage

_MODEL = PROJECT_ROOT / "ml/models/hardcases-20261022.joblib"
_CALIBRATION = PROJECT_ROOT / "ml/models/hardcases-20261022.abstention.json"
_SOURCE = PROJECT_ROOT / "ml/data/synthetic/ml/hardcases-20261022.json"
_MODES: tuple[Literal["rapidfuzz", "tfidf", "ml"], ...] = (
    "rapidfuzz", "tfidf", "ml",
)


def _load_frozen_model() -> tuple[TrustedModel, dict[str, float]]:
    model = load_trusted_artifact(_MODEL, expected_schema=FEATURE_SCHEMA)
    calibration = json.loads(_CALIBRATION.read_text(encoding="utf-8"))
    if (
        model.dataset_sha256 != hashlib.sha256(_SOURCE.read_bytes()).hexdigest()
        or not isinstance(calibration, dict)
        or calibration.get("dataset_sha256") != model.dataset_sha256
        or calibration.get("model_sha256") != hashlib.sha256(_MODEL.read_bytes()).hexdigest()
        or calibration.get("test_used_for_threshold_selection") is not False
        or calibration.get("production_eligible") is not False
    ):
        raise ValueError("Frozen model and calibration provenance mismatch")
    thresholds: dict[str, float] = {}
    for name in ("ml", "tfidf"):
        raw = calibration["models"][name]["threshold"]
        if type(raw) is not float or not math.isfinite(raw) or not 0 <= raw <= 1:
            raise ValueError("Unsafe or recalibrated confidence threshold")
        thresholds[name] = raw
    return model, thresholds


def _catalog_at_size(
    base: Sequence[CatalogProduct], count: int,
) -> tuple[CatalogProduct, ...]:
    if len(base) != 183 or count not in (1000, 5000):
        raise ValueError("Unexpected synthetic audit catalog or size")
    products = list(base)
    for offset in range(count - len(base)):
        source = base[offset % len(base)]
        item = source.model_dump(mode="python")
        item["sku"] = f"diagnostic-{offset+1:05d}"
        item["name"] = source.name + f" синтетический вариант {offset+1:05d}"
        products.append(CatalogProduct.model_validate(item))
    if len({item.sku for item in products}) != count:
        raise AssertionError("Synthetic SKU distractor IDs collided")
    if {item.category for item in products} != set(CATEGORY_ATTRIBUTES):
        raise AssertionError("Synthetic SKU stress misses taxonomy categories")
    return tuple(products)


def _negative_cases(
    originals: Sequence[CatalogProduct],
) -> dict[str, RequestedItem]:
    by_category = {category: next(
        (product for product in originals if product.category == category), None
    ) for category in sorted(CATEGORY_ATTRIBUTES)}
    outcome: dict[str, RequestedItem] = {}
    for category, product in by_category.items():
        if product is None:
            raise AssertionError("Synthetic dataset does not cover every category")
        extra: dict[str, Any] = {}
        if category == "paper":
            extra["paper_format"] = "never-existing-Z999"
        elif category == "pen":
            extra["pen_ink_color"] = "never-existing-color"
        else:
            rules = [
                field for field in CATEGORY_ATTRIBUTES[category]
                if field.name not in ("printer_model", "compatible_models")
            ]
            if not rules:
                continue
            chosen = rules[0]
            if chosen.value_kind is str:
                invalid: str | int = "nonexistent-compatibility-value-2026"
            elif chosen.value_kind is int:
                invalid = 999_999
            else:
                continue
            extra["attributes"] = {chosen.name: invalid}
        request = RequestedItem.model_validate({
            "category": category,
            "description": product.name + " несовместимая модель и параметры",
            "unit": "штука", "quantity": 1, **extra,
        })
        if not all(
            check_constraints(request, other).status == "incompatible"
            for other in originals
        ):
            # Fail closed: never mark a possibly present product as absent.
            continue
        outcome[category] = request
    if len(outcome) < 20:
        raise ValueError("Insufficient strictly proven no-match categories")
    return outcome


def _positive_cases(
    body: Mapping[str, Any], *, count: int,
) -> list[tuple[str, RequestedItem, frozenset[str]]]:
    records = body["queries"]
    if not isinstance(records, list) or len(records) != 439:
        raise ValueError("A46 source queries changed")
    grouped: dict[str, list[tuple[str, RequestedItem, frozenset[str]]]] = defaultdict(list)
    seen_ids: set[str] = set()
    for row in records:
        query_id = row["query_id"]
        request = RequestedItem.model_validate(row["request"])
        gold = frozenset(row["relevant_skus"])
        if not gold or query_id in seen_ids:
            raise ValueError("Invalid positive SKU label or duplicate query ID")
        seen_ids.add(query_id)
        grouped[request.category].append((query_id, request, gold))
    if set(grouped) != set(CATEGORY_ATTRIBUTES):
        raise ValueError("A46 positive groups miss a taxonomy category")
    if count == 1000:
        selected = [item for category in sorted(grouped) for item in grouped[category]]
    else:
        # Two original wordings from every category; never use different
        # subsets for different algorithms within this same catalog size.
        selected = [
            item for category in sorted(grouped)
            for item in sorted(grouped[category], key=lambda row: row[0])[:2]
        ]
    if count == 5000 and len(selected) != 80:
        raise ValueError("A46 5000-SKU sample must contain exactly 80 requests")
    return selected


def diagnostic_for_size(
    *, size: int,
    originals: Sequence[CatalogProduct],
    body: Mapping[str, Any],
    model: TrustedModel,
    thresholds: Mapping[str, float],
) -> dict[str, Any]:
    catalog = _catalog_at_size(originals, size)
    positives = _positive_cases(body, count=size)
    negatives = _negative_cases(originals)
    if any(
        retrieve_candidates(request, catalog, limit=20)
        for request in negatives.values()
    ):
        raise AssertionError("Declared no-match request is not empty for enlarged catalog")
    stats: dict[str, dict[str, Any]] = {
        name: {
            "top1": 0, "recall5": 0, "auto_correct": 0,
            "auto_wrong": 0, "auto_abstained": 0,
            "false_positive_on_proven_absence": 0,
            "rank_ms": [], "per_category": defaultdict(
                lambda: {"queries": 0, "top1": 0, "recall5": 0}
            ),
        }
        for name in _MODES
    }
    retrieval_ms: list[float] = []
    for _identifier, request, gold in positives:
        started = time.perf_counter_ns()
        shortlist = retrieve_candidates(request, catalog, limit=20)
        retrieval_ms.append((time.perf_counter_ns() - started) / 1_000_000)
        for mode in _MODES:
            timer = time.perf_counter_ns()
            ranked = rank_candidates(
                request, shortlist, mode=mode,
                artifact=model if mode == "ml" else None,
            )
            stats[mode]["rank_ms"].append((time.perf_counter_ns()-timer)/1_000_000)
            predictions = [candidate.product.sku for candidate in ranked]
            if len(set(predictions)) != len(predictions):
                raise AssertionError("Duplicate ranked SKUs")
            first_correct = bool(predictions and predictions[0] in gold)
            hit5 = bool(gold.intersection(predictions[:5]))
            current = stats[mode]
            current["top1"] += int(first_correct)
            current["recall5"] += int(hit5)
            cell = current["per_category"][request.category]
            cell["queries"] += 1
            cell["top1"] += int(first_correct)
            cell["recall5"] += int(hit5)
            if mode in thresholds:
                accepted = bool(ranked and ranked[0].score >= thresholds[mode])
                current["auto_correct"] += int(accepted and first_correct)
                current["auto_wrong"] += int(accepted and not first_correct)
                current["auto_abstained"] += int(not accepted)
    for request in negatives.values():
        shortlist = retrieve_candidates(request, catalog, limit=20)
        if shortlist:
            raise AssertionError("Proven-negative retrieved candidate")
        for mode in _MODES:
            ranked = rank_candidates(
                request, shortlist, mode=mode,
                artifact=model if mode == "ml" else None,
            )
            stats[mode]["false_positive_on_proven_absence"] += int(bool(ranked))
    report: dict[str, Any] = {}
    for mode_name, raw in stats.items():
        samples = len(positives)
        times: list[float] = raw["rank_ms"]
        report[mode_name] = {
            "positive_queries": samples,
            "top1_accuracy": round(raw["top1"]/samples, 6),
            "recall_at_5": round(raw["recall5"]/samples, 6),
            "top1_correct": raw["top1"],
            "retrieval_hit5": raw["recall5"],
            "frozen_threshold": thresholds.get(mode_name),
            "threshold_applied_to_positives": mode_name in thresholds,
            "auto_correct": raw["auto_correct"] if mode_name in thresholds else None,
            "auto_wrong": raw["auto_wrong"] if mode_name in thresholds else None,
            "auto_abstained": raw["auto_abstained"] if mode_name in thresholds else None,
            "proven_no_match_queries": len(negatives),
            "false_positive_on_proven_absence": raw["false_positive_on_proven_absence"],
            "rank_p50_ms": round(statistics.median(times), 3),
            "rank_max_ms": round(max(times), 3),
            "per_category": dict(sorted(raw["per_category"].items())),
        }
    return {
        "catalog_skus": size,
        "original_synthetic_catalog_skus": len(originals),
        "derived_synthetic_distractor_skus": size-len(originals),
        "proven_negative_categories": sorted(negatives),
        "query_selection": "all_439" if size == 1000 else "two_per_category_80",
        "candidate_shortlist_limit": 20,
        "retrieval_p50_ms": round(statistics.median(retrieval_ms), 3),
        "retrieval_max_ms": round(max(retrieval_ms), 3),
        "models": report,
    }


def build_multicategory_retrieval_report() -> dict[str, Any]:
    body = build_taxonomy_coverage()
    provenance = body.get("provenance")
    if not isinstance(provenance, dict) or (
        provenance.get("kind") != "synthetic_example"
        or provenance.get("independent_gold") is not False
        or provenance.get("production_training") is not False
    ):
        raise ValueError("A46 dataset must remain non-independent synthetic-only")
    originals = tuple(CatalogProduct.model_validate(item) for item in body["catalog"])
    if len(originals) != 183:
        raise ValueError("Synthetic source catalog changed")
    model, thresholds = _load_frozen_model()
    diagnostics = [
        diagnostic_for_size(
            size=size, originals=originals, body=body,
            model=model, thresholds=thresholds,
        )
        for size in (1000, 5000)
    ]
    result = {
        "kind": "kpdesk_synthetic_multicategory_ranking_diagnostic_v1",
        "production_eligible": False,
        "independent_customer_labels": False,
        "model_retrained": False,
        "llm_assisted_evaluated": False,
        "synthetic_dataset_sha256": hashlib.sha256(
            json.dumps(body, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "trained_model_sha256": hashlib.sha256(_MODEL.read_bytes()).hexdigest(),
        "training_source_sha256": model.dataset_sha256,
        "taxonomy_categories": len(CATEGORY_ATTRIBUTES),
        "sizes": diagnostics,
        "automatic_matching_release": {
            "eligible": False,
            "blocks": [
                "No independently verified customer gold labels or rights",
                "Pairwise ML thresholds calibrated on another synthetic corpus",
                "Accepted wrong top-1 suggestions on diagnostic positive queries",
                "No independently adjudicated semantic no-match examples",
            ],
            "positive_auto_error_counts": {
                str(size["catalog_skus"]): {
                    mode: size["models"][mode]["auto_wrong"]
                    for mode in ("ml", "tfidf")
                }
                for size in diagnostics
            },
        },
        "notes": (
            "All labels are synthetic; padding consists of source-name variants. "
            "No independent supplier rights, no actual LLM calls, no retraining. "
            "Hard negatives are deterministic incompatible attributes, "
            "not independently human-adjudicated semantic absence. "
            "ML/TFIDF thresholds belong to a different synthetic validation corpus. "
            "Wall times are local sequential warm-process diagnostics, not SLAs."
        ),
    }
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """Write one reproducible diagnostic JSON to an existing artifact directory."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    target = args.output.resolve()
    allowed = (PROJECT_ROOT / "artifacts").resolve()
    if (
        target.parent != allowed or target.suffix != ".json"
        or not allowed.is_dir() or allowed.is_symlink()
        or args.output.exists()
    ):
        raise ValueError("Diagnostics require a new JSON file in project artifacts")
    report = build_multicategory_retrieval_report()
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    for size in report["sizes"]:
        print(json.dumps({
            "catalog_skus": size["catalog_skus"],
            "positive_queries": size["models"]["ml"]["positive_queries"],
            "proven_negative_categories": len(size["proven_negative_categories"]),
            "retrieval_p50_ms": size["retrieval_p50_ms"],
            "ml": {key: value for key, value in size["models"]["ml"].items()
                   if key != "per_category"},
            "tfidf": {key: value for key, value in size["models"]["tfidf"].items()
                      if key != "per_category"},
            "rapidfuzz": {key: value for key, value in size["models"]["rapidfuzz"].items()
                          if key != "per_category"},
        }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
