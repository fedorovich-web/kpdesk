"""Reproducible SKU-level diagnostic, explicitly NOT independently labeled accuracy.

Train an in-memory pairwise LogisticRegression model only on authored training
queries. Freeze its ranking and per-mode validation-only acceptance thresholds
before scoring held-out authored synthetic requests. Never use Groq/network.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy
import sklearn

from kpdesk.catalog.parse import parse_price_list
from kpdesk.catalog.schema import ImportLimits
from kpdesk.matching.artifact import TrustedModel
from kpdesk.matching.constraints import check_constraints
from kpdesk.matching.detailed_metrics import detailed_ranking_metrics
from kpdesk.matching.features import FEATURE_SCHEMA
from kpdesk.matching.normalize import normalize_text
from kpdesk.matching.rank import rank_candidates
from kpdesk.matching.retrieve import retrieve_candidates
from kpdesk.matching.schema import CatalogProduct, EvaluationCase, RequestedItem
from kpdesk.matching.training import (
    DatasetSplit,
    LabeledQuery,
    TrainingConfig,
    train_reranker,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "ml/data/synthetic/portfolio-sku-benchmark-v1.json"
SOURCE = ROOT / "frontend/public/templates/kpdesk-portfolio-synthetic-v1.csv"
type Mode = Literal["rapidfuzz", "tfidf", "ml"]
MODES: tuple[Mode, ...] = ("rapidfuzz", "tfidf", "ml")
SPLITS = ("train", "validation", "test")
# A score gate for lexical baselines is a diagnostic heuristic, not
# a calibrated probability, and must not be used as production policy.
THRESHOLDS = tuple(index / 100 for index in range(101)) + (1.000001,)


@dataclass(frozen=True, slots=True)
class Prediction:
    query_id: str
    expected: frozenset[str]
    ranked_skus: tuple[str, ...]
    top_sku: str | None
    top_score: float | None
    top_status: str | None
    elapsed_ms: float


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_dataset(path: Path, products: Mapping[str, CatalogProduct]) -> DatasetSplit:
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("SKU dataset missing or over size limit")
    data = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != "synthetic-portfolio-sku-v1"
        or data.get("source") != "frontend/public/templates/kpdesk-portfolio-synthetic-v1.csv"
        or not isinstance(data.get("cases"), list)
    ):
        raise ValueError("Unsupported SKU dataset manifest")
    partitions: dict[str, list[LabeledQuery]] = {key: [] for key in SPLITS}
    query_ids: set[str] = set()
    group_owner: dict[str, str] = {}
    text_owner: dict[str, str] = {}
    for case in data["cases"]:
        if not isinstance(case, dict):
            raise TypeError("Malformed SKU case")
        split = case.get("split")
        if not isinstance(split, str) or split not in partitions:
            raise ValueError("Unknown query split")
        query_id, group = case.get("query_id"), case.get("query_group_id")
        if (
            not isinstance(query_id, str) or not query_id
            or not isinstance(group, str) or not group
            or query_id in query_ids
        ):
            raise ValueError("Duplicate or missing query identity")
        query_ids.add(query_id)
        previous_split = group_owner.setdefault(group, split)
        if previous_split != split:
            raise ValueError("Leaked query group across splits")
        raw = case.get("request")
        gold = case.get("relevant_skus")
        if not isinstance(raw, dict) or not isinstance(gold, list) or (
            not all(isinstance(sku, str) and sku for sku in gold)
            or len(set(gold)) != len(gold)
        ):
            raise ValueError("Malformed labeled request")
        request = RequestedItem.model_validate(raw)
        normalized = normalize_text(request.description)
        previous_group = text_owner.setdefault(normalized, group)
        if previous_group != group:
            raise ValueError("Leaked identical request text across groups")
        relevant = frozenset(gold)
        if not relevant <= products.keys():
            raise ValueError("Gold SKU missing from evaluation catalog")
        if any(check_constraints(request, products[sku]).status == "incompatible"
               for sku in relevant):
            raise ValueError("Gold SKU contradicts mandatory constraints")
        partitions[split].append(LabeledQuery(
            query_id=query_id, query_group_id=group,
            request=request, relevant_skus=relevant,
        ))
    if (
        not all(partitions.values())
        or any(not any(q.relevant_skus for q in partitions[split]) for split in SPLITS)
        or any(not any(not q.relevant_skus for q in partitions[split]) for split in SPLITS)
        or len(partitions["test"]) < 10
    ):
        raise ValueError("Insufficient positive/no-match split coverage")
    return DatasetSplit(
        train=tuple(partitions["train"]),
        validation=tuple(partitions["validation"]),
        test=tuple(partitions["test"]),
    )


def _synthetic_catalog() -> dict[str, CatalogProduct]:
    source = parse_price_list(SOURCE.name, SOURCE.read_bytes(), ImportLimits())
    if not source.publishable or len(source.products) != 5:
        raise ValueError("Portfolio source fixture changed or became invalid")
    products = {product.sku: product for product in source.products}
    if len(products) != 5:
        raise ValueError("Duplicate source SKU")
    # Two deliberately difficult, *invented* lookalikes per source product:
    # share safety attributes, differ in commercial product subtype.
    # This is an artificial ranking exercise, not supplier ground truth.
    for product in source.products:
        for number, kind in enumerate(("специальный образец", "набор для этикеток"), 1):
            sku = f"{product.sku}-D{number}"
            if sku in products:
                raise ValueError("Synthetic decoy SKU collision")
            products[sku] = product.model_copy(update={
                "sku": sku,
                "name": f"{kind} {product.name}",
            })
    if len(products) != 15:
        raise ValueError("Unexpected synthetic catalog size")
    return products


def _predict(
    queries: Sequence[LabeledQuery], products: Sequence[CatalogProduct],
    mode: Mode, model: TrustedModel,
) -> tuple[Prediction, ...]:
    predicted: list[Prediction] = []
    for record in queries:
        begin = time.perf_counter_ns()
        retrieved = retrieve_candidates(record.request, products, limit=20)
        ranked = rank_candidates(
            record.request, retrieved, mode, model if mode == "ml" else None,
        )
        elapsed = (time.perf_counter_ns() - begin) / 1e6
        eligible_top = next(
            (candidate for candidate in ranked if candidate.constraints.status == "compatible"),
            None,
        )
        predicted.append(Prediction(
            query_id=record.query_id,
            expected=record.relevant_skus,
            ranked_skus=tuple(candidate.product.sku for candidate in ranked),
            top_sku=eligible_top.product.sku if eligible_top else None,
            top_score=eligible_top.score if eligible_top else None,
            top_status=eligible_top.constraints.status if eligible_top else None,
            elapsed_ms=elapsed,
        ))
    return tuple(predicted)


def _selected_sku(prediction: Prediction, threshold: float) -> str | None:
    if (
        prediction.top_sku is None or prediction.top_status != "compatible"
        or prediction.top_score is None or not math.isfinite(prediction.top_score)
        or prediction.top_score < threshold
    ):
        return None
    return prediction.top_sku


def _calibrate(validation: Sequence[Prediction]) -> float:
    if not validation or not any(row.expected for row in validation) or (
        not any(not row.expected for row in validation)
    ):
        raise ValueError("Validation must contain positives and no-match cases")
    candidates: list[tuple[int, int, float]] = []
    for threshold in THRESHOLDS:
        accepted = [_selected_sku(row, threshold) for row in validation]
        mistakes = sum(
            bool(sku) and sku not in row.expected
            for row, sku in zip(validation, accepted, strict=True)
        )
        correct = sum(
            bool(sku) and sku in row.expected
            for row, sku in zip(validation, accepted, strict=True)
        )
        if mistakes == 0:
            candidates.append((correct, sum(bool(sku) for sku in accepted), threshold))
    if not candidates:
        raise RuntimeError("No safe validation-only threshold")
    # Coverage prioritized only among thresholds with *zero* validation false accepts.
    winner = max(candidates, key=lambda item: (item[0], item[1], -item[2]))
    return winner[2]


def _score(queries: Sequence[LabeledQuery], predictions: Sequence[Prediction],
           threshold: float) -> dict[str, Any]:
    if len(queries) != len(predictions) or not queries:
        raise ValueError("Mismatched ranking test cases")
    identities = [row.query_id for row in queries]
    if identities != [row.query_id for row in predictions] or len(set(identities)) != len(identities):
        raise ValueError("Mismatched or duplicate prediction identities")
    ranking = detailed_ranking_metrics(
        [EvaluationCase(q.query_id, q.relevant_skus) for q in queries],
        {p.query_id: list(p.ranked_skus) for p in predictions}, k=5,
    )
    selected = [_selected_sku(row, threshold) for row in predictions]
    accepted = sum(sku is not None for sku in selected)
    errors = sum(
        sku is not None and sku not in row.relevant_skus
        for row, sku in zip(queries, selected, strict=True)
    )
    correct = accepted - errors
    no_match_count = sum(not row.relevant_skus for row in queries)
    rejected_no_match = sum(
        not row.relevant_skus and sku is None
        for row, sku in zip(queries, selected, strict=True)
    )
    sorted_ms = sorted(row.elapsed_ms for row in predictions)
    p95 = sorted_ms[math.ceil(len(sorted_ms) * .95) - 1]
    return {
        "test_queries": len(queries),
        "positive_queries": ranking.positive_queries,
        "no_match_queries": no_match_count,
        "recall_at_5": round(ranking.hit_at_k / ranking.positive_queries, 6),
        "top_1_accuracy": round(ranking.hit_at_1 / ranking.positive_queries, 6),
        "accepted": accepted,
        "correct_accepted": correct,
        "false_automatic_matches": errors,
        "false_accepts_on_no_match": no_match_count - rejected_no_match,
        "positive_automatic_coverage": round(
            sum(
                bool(sku) and sku in row.relevant_skus
                for row, sku in zip(queries, selected, strict=True)
            ) / ranking.positive_queries, 6,
        ),
        "no_match_queries_with_nonempty_candidates": ranking.false_suggestions_on_no_match,
        "no_match_queries_with_empty_candidates": ranking.correct_empty_no_match,
        "accepted_precision": round(correct / accepted, 6) if accepted else None,
        "false_accept_rate_among_accepted": round(errors / accepted, 6) if accepted else None,
        "coverage_all_queries": round(accepted / len(queries), 6),
        "no_match_rejection_rate": round(rejected_no_match / no_match_count, 6),
        "latency_p95_ms": round(p95, 3),
        "latency_measurement": "single-process offline wall time, tiny synthetic test only",
        "cost_rub": None,
    }


def run_benchmark(root: Path = ROOT) -> dict[str, Any]:
    if root != ROOT:
        raise ValueError("Only the fixed trusted project data root is supported")
    products = _synthetic_catalog()
    split = _load_dataset(FIXTURE, products)
    trained = train_reranker(
        split, TrainingConfig(products=products, seed=42, max_candidates=20),
    )
    model = TrustedModel(
        pipeline=trained.pipeline, feature_schema=FEATURE_SCHEMA,
        dataset_sha256=_hash(FIXTURE), split_seed=42,
        training_data_kind="synthetic_example",
    )
    catalog = list(products.values())
    modes: dict[str, Any] = {}
    for mode in MODES:
        val_predictions = _predict(split.validation, catalog, mode, model)
        threshold = _calibrate(val_predictions)
        test_predictions = _predict(split.test, catalog, mode, model)
        modes[mode] = {
            "status": "measured_synthetic_only",
            "threshold_selected_on": "validation_only",
            "validation_threshold": threshold,
            "validation_cases": len(split.validation),
            "test": _score(split.test, test_predictions, threshold),
        }
    return {
        "schema_version": "portfolio-sku-benchmark-v1",
        "provenance": "authored synthetic examples; not independent human gold",
        "source_sha256": _hash(SOURCE),
        "queries_sha256": _hash(FIXTURE),
        "catalog": "5 synthetic portfolio SKU + 10 synthetic hard-negative lookalikes",
        "catalog_sku_count": len(products),
        "train_cases": len(split.train),
        "validation_cases": len(split.validation),
        "test_cases": len(split.test),
        "test_gold_is_independent_human_reviewed": False,
        "source_disjoint_evaluation": False,
        "training_pair_count": trained.train_pairs,
        "trained_ml_model": "in-memory sklearn LogisticRegression on pair features, not production artifact",
        "threshold_policy": "validation-only; zero false automatic matches on synthetic validation",
        "python": platform.python_version(),
        "scikit_learn": sklearn.__version__,
        "numpy": numpy.__version__,
        "models": {
            **modes,
            "llm_assisted": {
                "status": "not_run",
                "reason": "No authorized real provider evaluation; no cost, tokens or valid paired outputs",
                "test": None,
            },
        },
        "publishable_real_quality_claim": False,
        "limitations": [
            "tiny authored synthetic cases with reused underlying catalog",
            "same RapidFuzz top-20 candidate retrieval for all three measured rerankers",
            "synthetic hard negatives can bias decision threshold; no statistical confidence",
            "no LLM inference, extraction evaluation, provider cost or token usage",
            "not real supplier/domain holdout; no production API p95",
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("Evidence already exists; no overwrite permitted")
    report = run_benchmark()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2, sort_keys=True)
        file.write("\n")
    print(json.dumps({
        "schema_version": report["schema_version"],
        "train_cases": report["train_cases"],
        "validation_cases": report["validation_cases"],
        "test_cases": report["test_cases"],
        "models": report["models"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
