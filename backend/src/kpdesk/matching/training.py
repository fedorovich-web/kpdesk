"""Group-isolated pairwise training, using retrieved candidates only."""

from __future__ import annotations

import json
import random
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import ceil

from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline

from kpdesk.matching.constraints import check_constraints
from kpdesk.matching.features import PairFeatureExtractor, PairInput
from kpdesk.matching.metrics import evaluate_ranking
from kpdesk.matching.normalize import normalize_text
from kpdesk.matching.retrieve import retrieve_candidates
from kpdesk.matching.schema import (
    CatalogProduct,
    EvaluationCase,
    RankingMetrics,
    RequestedItem,
)


@dataclass(frozen=True, slots=True)
class LabeledQuery:
    query_id: str
    query_group_id: str
    request: RequestedItem
    relevant_skus: frozenset[str]


@dataclass(frozen=True, slots=True)
class DatasetSplit:
    train: tuple[LabeledQuery, ...]
    validation: tuple[LabeledQuery, ...]
    test: tuple[LabeledQuery, ...]


@dataclass(frozen=True, slots=True)
class TrainingConfig:
    products: Mapping[str, CatalogProduct]
    seed: int = 42
    max_candidates: int = 20
    explicit_hard_negatives: Mapping[str, frozenset[str]] | None = None
    reviewed_only: bool = False


@dataclass(frozen=True, slots=True)
class TrainResult:
    pipeline: Pipeline
    train_pairs: int
    validation_metrics: RankingMetrics


def _validate_query_provenance(records: Sequence[LabeledQuery]) -> None:
    """Reject leakage and contradictory labels, including caller-provided splits."""
    query_ids: set[str] = set()
    normalized_text_groups: dict[str, str] = {}
    request_labels: dict[str, frozenset[str]] = {}
    for record in records:
        if not record.query_id:
            raise ValueError("Missing query ID.")
        if record.query_id in query_ids:
            raise ValueError("Duplicate query ID: potential train/test leakage.")
        query_ids.add(record.query_id)
        if not record.query_group_id:
            raise ValueError("Missing query group ID.")
        normalized = normalize_text(record.request.description)
        assigned = normalized_text_groups.get(normalized)
        if assigned is not None and assigned != record.query_group_id:
            raise ValueError("cross-group text leakage: duplicated request text")
        normalized_text_groups[normalized] = record.query_group_id
        # Labels describe the complete typed request. Equal wording alone is
        # insufficient: A3 and A4, brands or units may legitimately differ.
        request_data = record.request.model_dump(mode='json')
        request_data['description'] = normalized
        request_key = json.dumps(request_data, sort_keys=True, ensure_ascii=False)
        previous_labels = request_labels.setdefault(request_key, record.relevant_skus)
        if previous_labels != record.relevant_skus:
            raise ValueError('Conflicting relevance labels for identical request.')


def split_groups(
    records: Sequence[LabeledQuery],
    seed: int,
) -> DatasetSplit:
    """Split by query group before fitting or hyperparameter selection."""
    _validate_query_provenance(records)
    groups: dict[str, list[LabeledQuery]] = defaultdict(list)
    for record in records:
        groups[record.query_group_id].append(record)
    if len(groups) < 3:
        raise ValueError("At least three independent query groups are required.")

    shuffled = sorted(groups)
    random.Random(seed).shuffle(shuffled)
    n = len(shuffled)
    holdout = max(1, ceil(n * 0.2))
    if holdout * 2 >= n:
        holdout = 1
    train_groups = shuffled[: n - 2 * holdout]
    val_groups = shuffled[n - 2 * holdout : n - holdout]
    test_groups = shuffled[n - holdout :]

    def flatten(identifiers: list[str]) -> tuple[LabeledQuery, ...]:
        return tuple(record for identifier in identifiers for record in groups[identifier])

    return DatasetSplit(
        train=flatten(train_groups),
        validation=flatten(val_groups),
        test=flatten(test_groups),
    )


def _pairs_for_training(
    queries: Sequence[LabeledQuery], products: Sequence[CatalogProduct], limit: int,
    *,
    explicit_negatives: Mapping[str, frozenset[str]] | None = None,
    reviewed_only: bool = False,
) -> tuple[list[PairInput], list[int]]:
    pairs: list[PairInput] = []
    labels: list[int] = []
    indexed = {product.sku: product for product in products}
    for example in queries:
        already_seen: set[str] = set()
        reviewed_negative_skus = (explicit_negatives or {}).get(example.query_id, frozenset())
        if reviewed_only and not reviewed_negative_skus:
            raise ValueError("Reviewed negative labels missing for training query.")
        for candidate in retrieve_candidates(example.request, products, limit):
            already_seen.add(candidate.product.sku)
            if (reviewed_only and candidate.product.sku not in example.relevant_skus
                    and candidate.product.sku not in reviewed_negative_skus):
                continue  # Do not manufacture negative labels from unknown candidates.
            pairs.append(PairInput(example.request, candidate.product))
            labels.append(int(candidate.product.sku in example.relevant_skus))
        if reviewed_only and not example.relevant_skus <= already_seen:
            raise ValueError(
                "Reviewed positive not retrievable: improve candidate retrieval first."
            )
        for sku in sorted((explicit_negatives or {}).get(example.query_id, frozenset())):
            if sku in example.relevant_skus:
                raise ValueError("Reviewed negative conflicts with positive label")
            if sku not in indexed:
                raise ValueError("Reviewed negative references unknown SKU")
            if sku not in already_seen:
                pairs.append(PairInput(example.request, indexed[sku]))
                labels.append(0)
    return pairs, labels


def evaluate_reranker(
    pipeline: Pipeline, queries: Sequence[LabeledQuery],
    products: Sequence[CatalogProduct], limit: int,
) -> RankingMetrics:
    cases: list[EvaluationCase] = []
    ranked: dict[str, list[str]] = {}
    for example in queries:
        candidates = retrieve_candidates(example.request, products, limit=limit)
        pairs = [PairInput(example.request, candidate.product) for candidate in candidates]
        if pairs:
            scores = pipeline.predict_proba(pairs)[:, 1]
            scored = sorted(
                zip(candidates, scores, strict=True),
                key=lambda item: (
                    item[0].constraints.status != "compatible",
                    -float(item[1]),
                    item[0].product.sku,
                ),
            )
            ranked[example.query_id] = [candidate.product.sku for candidate, _ in scored]
        else:
            ranked[example.query_id] = []
        cases.append(EvaluationCase(example.query_id, example.relevant_skus))
    return evaluate_ranking(cases, ranked, k=limit)


def train_reranker(split: DatasetSplit, config: TrainingConfig) -> TrainResult:
    if not 1 <= config.max_candidates <= 20:
        raise ValueError("max_candidates must be within 1..20")
    if not split.train or not split.validation or not split.test:
        raise ValueError("All three data splits are required.")

    products = list(config.products.values())
    if not products or any(sku != item.sku for sku, item in config.products.items()):
        raise ValueError("Missing or mismatched product catalog.")
    all_splits = (split.train, split.validation, split.test)
    all_queries = (*split.train, *split.validation, *split.test)
    _validate_query_provenance(all_queries)
    seen_groups: set[str] = set()
    negatives = config.explicit_hard_negatives or {}
    if set(negatives) - {record.query_id for record in all_queries}:
        raise ValueError("Reviewed negatives reference unknown query ID.")
    for partition in all_splits:
        groups = {record.query_group_id for record in partition}
        if not groups or seen_groups & groups:
            raise ValueError("ML train/validation/test group overlap or missing groups.")
        seen_groups.update(groups)
        for record in partition:
            if not record.relevant_skus <= config.products.keys():
                raise ValueError("Gold contains unknown catalog SKU.")
            for sku in record.relevant_skus:
                if check_constraints(record.request, config.products[sku]).status == "incompatible":
                    raise ValueError("Gold positive conflicts with hard constraints.")
            reviewed = negatives.get(record.query_id, frozenset())
            if config.reviewed_only and not reviewed:
                raise ValueError("Reviewed negative labels missing for query.")
            if reviewed & record.relevant_skus:
                raise ValueError("Reviewed negative conflicts with positive label")
            if not reviewed <= config.products.keys():
                raise ValueError("Reviewed negative references unknown SKU")
    pairs, labels = _pairs_for_training(
        split.train, products, config.max_candidates,
        explicit_negatives=config.explicit_hard_negatives,
        reviewed_only=config.reviewed_only,
    )
    if set(labels) != {0, 1}:
        raise ValueError("Training requires both relevant and irrelevant retrieved pairs.")

    pipeline = Pipeline(
        [
            ("pair_features", PairFeatureExtractor()),
            ("classifier", LogisticRegression(
                class_weight="balanced",
                max_iter=600,
                random_state=config.seed,
            )),
        ]
    )
    pipeline.fit(pairs, labels)
    return TrainResult(
        pipeline=pipeline,
        train_pairs=len(pairs),
        validation_metrics=evaluate_reranker(
            pipeline, split.validation, products, config.max_candidates
        ),
    )
