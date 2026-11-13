"""Offline diagnostic of the pinned runtime; no training, calibration or providers.

The authored portfolio cases have been reused during development. They are NOT
blind or independent human ground truth, including their original 'test' split.
TF-IDF uses the existing runtime's per-query vectorization; no model is trained.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import statistics
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from kpdesk.config import PROJECT_ROOT
from kpdesk.llm.settings import LLMSettings as GroqSettings
from kpdesk.matching import runtime
from kpdesk.matching.constraints import check_constraints
from kpdesk.matching.rank import rank_candidates
from kpdesk.matching.retrieve import retrieve_candidates
from kpdesk.matching.runtime import load_local_ml_models
from kpdesk.quotes.document import ProposalNarrative
from scripts.benchmark_portfolio_sku import FIXTURE, SOURCE, _load_dataset, _synthetic_catalog

type Mode = Literal["rapidfuzz", "tfidf", "ml"]
MODES: tuple[Mode, ...] = ("rapidfuzz", "tfidf", "ml")


@dataclass(frozen=True, slots=True)
class DiagnosticPrediction:
    query_id: str
    expected: frozenset[str]
    ranked_skus: tuple[str, ...]
    elapsed_ms: float


def score_predictions(rows: Sequence[DiagnosticPrediction]) -> dict[str, Any]:
    if len({row.query_id for row in rows}) != len(rows):
        raise ValueError("Duplicate query ID")
    if any(len(set(row.ranked_skus)) != len(row.ranked_skus) for row in rows):
        raise ValueError("Duplicate predicted SKU")
    positive = [row for row in rows if row.expected]
    negative = [row for row in rows if not row.expected]
    top_hits = sum(bool(row.ranked_skus) and row.ranked_skus[0] in row.expected for row in positive)
    recovered = sum(len(set(row.ranked_skus[:5]) & row.expected) for row in positive)
    gold_count = sum(len(row.expected) for row in positive)
    nonempty = sum(bool(row.ranked_skus) for row in negative)
    elapsed = sorted(row.elapsed_ms for row in rows)
    return {
        "cases": len(rows), "positive_cases": len(positive),
        "top1": {"hits": top_hits, "denominator": len(positive),
                 "value": top_hits / len(positive) if positive else None},
        "recall5": {
            "retrieved_gold_skus": recovered, "gold_skus": gold_count,
            "positive_query_denominator": len(positive),
            "value": recovered / gold_count if gold_count else None,
            "macro_query_value": statistics.mean(
                len(set(row.ranked_skus[:5]) & row.expected) / len(row.expected)
                for row in positive
            ) if positive else None,
        },
        "no_match": {
            "cases": len(negative), "nonempty_suggestions": nonempty,
            "empty_suggestions": len(negative) - nonempty,
            "nonempty_suggestion_rate": nonempty / len(negative) if negative else None,
        },
        "automatic_acceptance_measured": False,
        "ranking_ms": {
            "mean": round(statistics.mean(elapsed), 4) if elapsed else None,
            "p95": round(elapsed[max(0, math.ceil(len(elapsed) * .95) - 1)], 4)
            if elapsed else None,
            "total": round(sum(elapsed), 4),
        },
    }


def llm_configuration(path: Path = PROJECT_ROOT / ".env") -> dict[str, Any]:
    # Use the exact runtime settings reader. Do not emit its object or errors:
    # ValidationError could contain an operator's key or arbitrary model text.
    try:
        settings = GroqSettings.from_local_env(path)
        configured, model = True, settings.model
    except (OSError, ValueError):
        configured, model = False, None
    return {
        "provider": "openrouter", "configured": configured, "model": model,
        "configuration_source": "process_env_or_root_dotenv_via_LLMSettings.from_local_env",
        "quality_status": "not_measured", "provider_calls": 0,
    }


def narrative_boundary_probe() -> dict[str, Any]:
    claim = "Гарантируем доставку за один день и сертифицированное качество."
    try:
        ProposalNarrative(headline="Предложение", opening="Обсудим вашу задачу.",
                          solution=claim, closing="Согласуем состав заказа.")
        accepted = True
    except ValidationError:
        accepted = False
    return {
        "kind": "validator_boundary", "synthetic_claim": claim,
        "unsupported_claim_accepted_by_schema": accepted,
        "provider_behavior_measured": False,
        "interpretation": "Schema validates shape and price exclusion; it does not verify business facts.",
    }


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


def _model_metadata(name: str, expected: str) -> dict[str, Any]:
    path = runtime.MODEL_ROOT / name
    manifest = path.with_suffix(".joblib.manifest.json")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    return {
        "path": _relative(path), "sha256": _sha(path), "pinned_sha256": expected,
        "manifest_path": _relative(manifest), "manifest_sha256": _sha(manifest),
        "training_dataset_sha256": data["dataset_sha256"],
        "manifest_training_data_kind": data["training_data_kind"],
        "manifest_production_eligible": data["production_eligible"],
    }


def build_report() -> dict[str, Any]:
    started = time.perf_counter_ns()
    paths = [FIXTURE, SOURCE,
             runtime.MODEL_ROOT / runtime.RERANKER_NAME,
             runtime.MODEL_ROOT / runtime.CATEGORY_NAME]
    paths.extend([path.with_suffix(".joblib.manifest.json") for path in paths[2:]])
    original_hashes = {_relative(path): _sha(path) for path in paths}
    load_started = time.perf_counter_ns()
    models = load_local_ml_models()
    model_load_ms = (time.perf_counter_ns() - load_started) / 1e6
    products = _synthetic_catalog()
    split = _load_dataset(FIXTURE, products)
    partitions = {"train": split.train, "validation": split.validation, "test": split.test}
    records = [(partition, query) for partition, queries in partitions.items() for query in queries]
    predictions: dict[Mode, list[DiagnosticPrediction]] = {mode: [] for mode in MODES}
    case_details: list[dict[str, Any]] = []
    violations: dict[Mode, int] = {mode: 0 for mode in MODES}
    retrieval_times: list[float] = []
    for partition, query in records:
        retrieval_started = time.perf_counter_ns()
        pool = retrieve_candidates(query.request, list(products.values()), limit=20)
        retrieval_ms = (time.perf_counter_ns() - retrieval_started) / 1e6
        retrieval_times.append(retrieval_ms)
        detail: dict[str, Any] = {
            "query_id": query.query_id, "original_split": partition,
            "expected_skus": sorted(query.relevant_skus),
            "retrieved_skus": [candidate.product.sku for candidate in pool],
            "rankings": {},
        }
        # Retrieve exactly once; no gold injection, mode-specific pool or threshold tuning.
        for mode in MODES:
            rank_started = time.perf_counter_ns()
            ranked = rank_candidates(query.request, pool, mode,
                                     models.reranker if mode == "ml" else None)
            elapsed = (time.perf_counter_ns() - rank_started) / 1e6
            predictions[mode].append(DiagnosticPrediction(
                query.query_id, query.relevant_skus,
                tuple(candidate.product.sku for candidate in ranked), elapsed,
            ))
            violations[mode] += sum(
                check_constraints(query.request, candidate.product).status == "incompatible"
                for candidate in ranked
            )
            detail["rankings"][mode] = [
                {"sku": candidate.product.sku, "score": round(candidate.score, 6),
                 "constraint_status": candidate.constraints.status}
                for candidate in ranked[:5]
            ]
        case_details.append(detail)
    modes = {}
    split_by_id = {query.query_id: partition for partition, query in records}
    for mode in MODES:
        modes[mode] = {
            "all_cases": score_predictions(predictions[mode]),
            "original_splits": {
                partition: score_predictions([
                    row for row in predictions[mode] if split_by_id[row.query_id] == partition
                ]) for partition in partitions
            },
            "constraint_violations": violations[mode],
        }
    reranker = _model_metadata(runtime.RERANKER_NAME, runtime.RERANKER_SHA256)
    reranker["loaded_training_data_kind"] = models.reranker.training_data_kind
    reranker["loaded_training_dataset_sha256"] = models.reranker.dataset_sha256
    reranker["feature_schema"] = models.reranker.feature_schema
    catalog_json = json.dumps(
        [products[sku].model_dump(mode="json") for sku in sorted(products)],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    unchanged = all(_sha(PROJECT_ROOT / path) == digest for path, digest in original_hashes.items())
    if not unchanged:
        raise RuntimeError("Diagnostic inputs changed during evaluation")
    return {
        "schema_version": "kpdesk-pinned-runtime-diagnostic-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "production_quality_confirmed": False,
        "real_world_accuracy": None,
        "runtime": {
            "loader": "kpdesk.matching.runtime.load_local_ml_models",
            "diagnostic_process_ml_opt_in": os.environ.get("KP_DESK_ENABLE_SYNTHETIC_ML") == "1",
            "flag_scope": "This diagnostic process only; running HTTP server state was not queried.",
            "runtime_api_requires_ml_opt_in": True,
            "category_classifier_evaluated": False,
            "reranker": reranker,
            "category_classifier": _model_metadata(runtime.CATEGORY_NAME, runtime.CATEGORY_SHA256),
            "model_load_ms": round(model_load_ms, 4), "python": platform.python_version(),
        },
        "dataset": {
            "path": _relative(FIXTURE), "sha256": _sha(FIXTURE),
            "catalog_source_path": _relative(SOURCE), "catalog_source_sha256": _sha(SOURCE),
            "catalog_skus": len(products),
            "derived_catalog_sha256": hashlib.sha256(catalog_json).hexdigest(),
            "derived_catalog_recipe": "benchmark_portfolio_sku._synthetic_catalog: 5 authored SKUs + 10 invented lookalikes",
            "cases": len(records), "provenance": "reused authored synthetic diagnostic",
            "independent_human_labels": False, "blind_holdout": False,
            "source_disjoint_evaluation": False,
            "original_split_names_are_not_new_holdout_evidence": True,
        },
        "method": {
            "retrieval": "one shared runtime retrieval pool per query; limit=20",
            "model_training": False, "threshold_tuning": False, "gold_injection": False,
            "existing_tfidf_per_query_vectorization": True,
            "timing_scope": "warm local diagnostic, sequential mode order; not API end-to-end or a load test",
            "no_match_nonempty_is_not_automatic_acceptance": True,
        },
        "modes": modes,
        "llm": llm_configuration(),
        "narrative_probe": narrative_boundary_probe(),
        "input_hashes": original_hashes,
        "inputs_unchanged_after_run": unchanged,
        "retrieval_total_ms": round(sum(retrieval_times), 4),
        "total_elapsed_ms": round((time.perf_counter_ns() - started) / 1e6, 4),
        "prior_evidence_references_not_refreshed": [
            "docs/portfolio/evidence/p2-real-benchmark-readiness-20261025.json",
            "docs/model-quality-evaluation.md",
        ],
        "limitations": [
            "Authored portfolio cases have already been used in development and cannot establish production accuracy.",
            "No fresh independent human SKU gold, supplier holdout or buyer-disjoint evaluation was performed.",
            "Pinned synthetic reranker availability does not prove a running server enables ML.",
            "OpenRouter credentials/model configuration does not measure extraction or proposal quality.",
            "No provider, HTTP server, database, sealed category holdout or training operation was called.",
        ],
        "cases": case_details,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    output: Path = args.output
    if output.exists():
        parser.error("Output exists; refusing to overwrite evidence.")
    report = build_report()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as destination:
        json.dump(report, destination, ensure_ascii=False, indent=2, allow_nan=False)
        destination.write("\n")
    print(json.dumps({"report": str(output), "production_quality_confirmed": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
