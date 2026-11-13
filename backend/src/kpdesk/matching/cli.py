"""Reproducible local CLI for labelled query/SKU training and one-shot test evaluation.

Usage:
  python -m kpdesk.matching.cli prepare --dataset ml/data/labels.json
  python -m kpdesk.matching.cli train --dataset ... --model ml/models/model.joblib
  python -m kpdesk.matching.cli evaluate --dataset ... --model ml/models/model.joblib

The input dataset is supplied by the user, not fetched or invented.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from kpdesk.matching.artifact import (
    DEFAULT_TRUSTED_ROOT,
    TrustedModel,
    load_trusted_artifact,
    save_trusted_artifact,
)
from kpdesk.matching.detailed_metrics import compare_lexical_baselines
from kpdesk.matching.features import FEATURE_SCHEMA
from kpdesk.matching.feedback_bundle import (
    MAX_FEEDBACK_BYTES,
    FeedbackBundle,
    dataset_from_feedback,
)
from kpdesk.matching.schema import CatalogProduct, RequestedItem
from kpdesk.matching.training import (
    DatasetSplit,
    LabeledQuery,
    TrainingConfig,
    evaluate_reranker,
    split_groups,
    train_reranker,
)


@dataclass(frozen=True, slots=True)
class TrainingDataset:
    source_sha256: str
    provenance_kind: Literal["synthetic_example", "independent_human_reviewed", "manager_reviewed"]
    products: dict[str, CatalogProduct]
    queries: tuple[LabeledQuery, ...]
    explicit_hard_negatives: dict[str, frozenset[str]]

    @property
    def independent_labels(self) -> bool:
        # A JSON provenance claim is not independently authenticated evidence.
        return False

    @property
    def declared_independent_labels(self) -> bool:
        return self.provenance_kind == "independent_human_reviewed"

    @property
    def requires_reviewed_negatives(self) -> bool:
        return self.provenance_kind in ('independent_human_reviewed', 'manager_reviewed')


def load_dataset(path: Path) -> TrainingDataset:
    if not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
        raise ValueError("Dataset file is missing or too large.")
    raw = path.read_bytes()
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError("Dataset must be valid UTF-8 JSON.") from error
    if not isinstance(body, dict):
        raise TypeError("Dataset must be a JSON object.")
    provenance = body.get("provenance")
    if not isinstance(provenance, dict) or not provenance.get("annotation_method"):
        raise ValueError("Dataset provenance is required.")
    kind = provenance.get("kind")
    if kind not in ("synthetic_example", "independent_human_reviewed", "manager_reviewed"):
        raise ValueError("Dataset provenance kind is unsupported.")

    catalog = body.get("catalog")
    cases = body.get("queries")
    if not isinstance(catalog, list) or not isinstance(cases, list):
        raise TypeError("Dataset must include catalog and queries lists.")
    if not catalog or len(catalog) > 5000 or len(cases) > 100_000:
        raise ValueError("Dataset sizes violate configured limits.")
    try:
        products_list = [CatalogProduct.model_validate(item) for item in catalog]
    except ValidationError as error:
        raise ValueError("Catalog contains invalid product data.") from error
    products = {p.sku: p for p in products_list}
    if len(products) != len(products_list):
        raise ValueError("Duplicate catalog SKU.")

    queries: list[LabeledQuery] = []
    reviewed_negatives: dict[str, frozenset[str]] = {}
    for item in cases:
        minimal = {"query_id", "query_group_id", "request", "relevant_skus"}
        permitted = minimal | {"reviewed_negative_skus"}
        if (not isinstance(item, dict)
                or not minimal <= set(item) or not set(item) <= permitted):
            raise ValueError("Invalid labelled query structure.")
        if kind in ("independent_human_reviewed", "manager_reviewed") and "reviewed_negative_skus" not in item:
            raise ValueError("Independent data require reviewed_negative_skus for each query.")
        relevant = item["relevant_skus"]
        if not isinstance(relevant, list) or not all(
            isinstance(sku, str) for sku in relevant
        ):
            raise ValueError("Gold SKU labels must be strings.")
        if set(relevant) - set(products):
            raise ValueError("Gold labels contain an unknown SKU.")
        raw_negatives = item.get("reviewed_negative_skus", [])
        if (not isinstance(raw_negatives, list) or not all(
            isinstance(sku, str) for sku in raw_negatives
        )):
            raise ValueError("reviewed_negative_skus must contain strings.")
        negatives = frozenset(raw_negatives)
        if len(negatives) != len(raw_negatives):
            raise ValueError("Duplicate reviewed_negative_skus.")
        if negatives - set(products):
            raise ValueError("Reviewed negative contains unknown SKU.")
        if negatives & set(relevant):
            raise ValueError("Reviewed negative conflicts with positive label.")
        if kind in ("independent_human_reviewed", "manager_reviewed") and not negatives:
            raise ValueError("Independent data require nonempty reviewed_negative_skus.")
        if not isinstance(item["query_id"], str) or not isinstance(item["query_group_id"], str):
            raise TypeError("Query IDs must be strings.")
        try:
            request = RequestedItem.model_validate(item["request"])
        except ValidationError as error:
            raise ValueError("Invalid labelled request.") from error
        queries.append(LabeledQuery(
            query_id=item["query_id"],
            query_group_id=item["query_group_id"],
            request=request,
            relevant_skus=frozenset(relevant),
        ))
        reviewed_negatives[item["query_id"]] = negatives
    return TrainingDataset(
        source_sha256=hashlib.sha256(raw).hexdigest(),
        provenance_kind=kind,
        products=products,
        queries=tuple(queries),
        explicit_hard_negatives=reviewed_negatives,
    )


def _split_info(split: DatasetSplit) -> dict[str, int]:
    return {
        "train_queries": len(split.train),
        "validation_queries": len(split.validation),
        "test_queries": len(split.test),
        "train_groups": len({q.query_group_id for q in split.train}),
        "validation_groups": len({q.query_group_id for q in split.validation}),
        "test_groups": len({q.query_group_id for q in split.test}),
    }


def _write_report(path: Path, data: dict[str, Any]) -> None:
    # 'x' fails closed on a repeated test-evaluation attempt.
    with path.open("x", encoding="utf-8") as output:
        json.dump(data, output, ensure_ascii=False, sort_keys=True, indent=2)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KPDesk local labelled ML pipeline")
    sub = parser.add_subparsers(dest="command", required=True)
    feedback_command = sub.add_parser('feedback-dataset')
    feedback_command.add_argument('--feedback', type=Path, nargs='+', required=True)
    feedback_command.add_argument('--output', type=Path, required=True)
    for name in ("prepare", "train", "evaluate"):
        command = sub.add_parser(name)
        command.add_argument("--dataset", type=Path, required=True)
        if name in ("prepare", "train"):
            command.add_argument("--seed", type=int, default=42)
        if name in ("train", "evaluate"):
            command.add_argument("--model", type=Path, required=True)
            command.add_argument("--trusted-root", type=Path, default=DEFAULT_TRUSTED_ROOT)
    args = parser.parse_args(argv)
    if args.command == 'feedback-dataset':
        bundles = []
        if len(args.feedback) > 100:
            raise ValueError('At most 100 feedback bundles per batch.')
        for path in args.feedback:
            if not path.is_file() or path.stat().st_size > MAX_FEEDBACK_BYTES:
                raise ValueError('Feedback file missing or too large.')
            bundles.append(FeedbackBundle.model_validate_json(path.read_bytes()))
        dataset_body = dataset_from_feedback(bundles)
        if len(json.dumps(dataset_body, ensure_ascii=False).encode('utf-8')) > MAX_FEEDBACK_BYTES:
            raise ValueError('Feedback dataset is too large.')
        _write_report(args.output, dataset_body)
        print(json.dumps({'queries': len(dataset_body['queries']), 'training_data_kind': 'manager_reviewed',
                          'independent_labels': False, 'model_activated': False}))
        return 0
    dataset = load_dataset(args.dataset)

    if args.command == "evaluate":
        model = load_trusted_artifact(
            args.model, expected_schema=FEATURE_SCHEMA, trusted_root=args.trusted_root
        )
        if model.dataset_sha256 != dataset.source_sha256:
            raise ValueError("Dataset SHA256 differs from the trained model.")
        if model.training_data_kind != dataset.provenance_kind:
            raise ValueError("Dataset training provenance differs from the trained model.")
        split = split_groups(dataset.queries, seed=model.split_seed)
        report_path = args.model.with_suffix(".test-report.json")
        if report_path.exists():
            raise ValueError("Model test split was already evaluated.")
        metrics = evaluate_reranker(
            model.pipeline, split.test, list(dataset.products.values()), 20
        )
        baselines = compare_lexical_baselines(
            split.test, dataset.products, max_candidates=20,
        )
        report: dict[str, Any] = {
            "dataset_sha256": dataset.source_sha256,
            "evaluation_set": "test",
            "independent_labels": dataset.independent_labels,
            "declared_independent_labels": dataset.declared_independent_labels,
            "production_benchmark": False,
            "metrics": asdict(metrics),
            "lexical_baselines": {
                name: asdict(scores) for name, scores in baselines.items()
            },
            **_split_info(split),
        }
        _write_report(report_path, report)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0

    split = split_groups(dataset.queries, seed=args.seed)
    if args.command == "prepare":
        print(json.dumps({
            "dataset_sha256": dataset.source_sha256,
            "queries": len(dataset.queries),
            "independent_labels": dataset.independent_labels,
            "declared_independent_labels": dataset.declared_independent_labels,
            **_split_info(split),
        }, ensure_ascii=False, sort_keys=True))
        return 0

    trained = train_reranker(
        split,
        TrainingConfig(
            products=dataset.products,
            seed=args.seed,
            explicit_hard_negatives=dataset.explicit_hard_negatives,
            reviewed_only=dataset.requires_reviewed_negatives,
        ),
    )
    baselines = compare_lexical_baselines(
        split.validation, dataset.products, max_candidates=20,
    )
    saved = TrustedModel(
        pipeline=trained.pipeline,
        feature_schema=FEATURE_SCHEMA,
        dataset_sha256=dataset.source_sha256,
        split_seed=args.seed,
        training_data_kind=dataset.provenance_kind,
    )
    save_trusted_artifact(args.model, saved, trusted_root=args.trusted_root)
    print(json.dumps({
        "evaluation_set": "validation",
        "train_pairs": trained.train_pairs,
        "independent_labels": dataset.independent_labels,
            "declared_independent_labels": dataset.declared_independent_labels,
        "production_benchmark": False,
        "dataset_sha256": dataset.source_sha256,
        "metrics": asdict(trained.validation_metrics),
        "lexical_baselines": {
            name: asdict(scores) for name, scores in baselines.items()
        },
        **_split_info(split),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
