"""Offline source-locked candidate triage for *independent* procurement requests.

Candidate SKU and lexical proximity are proposals, NOT human relevance gold.
No model calls, training, catalog publication, or reuse-rights certification.
Locked test requests are deliberately excluded from the annotation queue.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from rapidfuzz import fuzz

from kpdesk.matching.normalize import normalize_text
from kpdesk.matching.real_requests import (
    RequestIntake,
    build_request_split_manifest,
)
from kpdesk.matching.schema import CatalogProduct


def build_request_candidate_queue(
    intake: RequestIntake,
    split_manifest: Mapping[str, object],
    catalog: Mapping[str, CatalogProduct],
    *,
    candidates_per_request: int = 6,
    max_requests: int = 50,
) -> dict[str, object]:
    """Prepare an unlabelled, bounded, source-verifiable human triage packet."""
    if not isinstance(intake, RequestIntake) or not intake.source_excerpts_verified:
        raise ValueError("Original request source excerpts must be verified.")
    if intake.training_approved or intake.independently_verified:
        raise ValueError("Unverified intake queue cannot impersonate independent approval.")
    if type(candidates_per_request) is not int or not 2 <= candidates_per_request <= 20:
        raise ValueError("Candidate limit must be an integer between 2 and 20.")
    if type(max_requests) is not int or not 1 <= max_requests <= 200:
        raise ValueError("Request queue limit must be an integer between 1 and 200.")
    if not isinstance(split_manifest, Mapping):
        raise TypeError("Source batch split manifest is required.")
    seed = split_manifest.get("seed")
    if type(seed) is not int:
        raise ValueError("Source batch split seed is invalid.")
    expected_split = build_request_split_manifest(intake, seed=seed)
    if dict(split_manifest) != expected_split:
        raise ValueError("Frozen request split manifest or source groups changed.")
    if any(record.origin_declared == "synthetic" for record in intake.records):
        raise ValueError("Synthetic request cannot enter independent-request review.")
    if not 2 <= len(catalog) <= 5000 or any(
        sku != product.sku or not isinstance(product, CatalogProduct)
        for sku, product in catalog.items()
    ):
        raise ValueError("Candidate catalog identity or size invalid.")

    # Fingerprint even non-displayed catalog facts, so a changed catalog cannot
    # silently inherit old candidate annotations or author-approved SKUs.
    canonical = json.dumps(
        [catalog[sku].model_dump(mode="json") for sku in sorted(catalog)],
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    catalog_digest = hashlib.sha256(canonical).hexdigest()
    partitions = expected_split["partitions"]
    allowed = set(partitions["train"]) | set(partitions["validation"])
    held_out = set(partitions["test"])
    if allowed & held_out:
        raise ValueError("Locked test IDs overlap annotation pool.")
    records = {record.request_id: record for record in intake.records}
    if allowed | held_out != set(records):
        raise ValueError("Request split does not cover exact source records.")

    tasks: list[dict[str, object]] = []
    for request_id in sorted(allowed)[:max_requests]:
        record = records[request_id]
        description = normalize_text(record.request_text)
        ranked = sorted(
            catalog.values(),
            key=lambda product: (
                -fuzz.WRatio(description, normalize_text(product.name)),
                product.sku,
            ),
        )[:candidates_per_request]
        tasks.append({
            "request_id": request_id,
            "query_group_id": record.buyer_group_id or record.source_batch_id,
            "source_batch_id": record.source_batch_id,
            "source_file": record.source_file,
            "source_sha256": record.source_sha256,
            "request_text": record.request_text,
            "partition": "train" if request_id in partitions["train"] else "validation",
            "review_state": "unreviewed",
            "candidate_origin": "lexical_proposals_not_labels",
            "candidate_skus": [product.sku for product in ranked],
            "reviewed_positive_skus": None,
            "reviewed_negative_skus": None,
            "reviewed_no_match": None,
        })
    return {
        "schema_version": "request-candidate-review-v1",
        "intake_sha256": intake.source_sha256,
        "catalog_sha256": catalog_digest,
        "frozen_split_sha256": hashlib.sha256(json.dumps(
            split_manifest, sort_keys=True, ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest(),
        "tasks": tasks,
        "excluded_test_queries": len(held_out),
        "source_excerpts_verified": True,
        "origin_independently_verified": False,
        "rights_verified": False,
        "privacy_review_complete": False,
        "catalog_reuse_rights_verified": False,
        "all_relevance_labels_reviewed": False,
        "independent_gold": False,
        "training_approved": False,
        "benchmark_approved": False,
    }


def verify_request_candidate_queue(
    intake: RequestIntake,
    split_manifest: Mapping[str, object],
    catalog: Mapping[str, CatalogProduct],
    queue: Mapping[str, object],
) -> bool:
    """Recompute exact tasks; tampered candidates cannot become trustworthy."""
    if not isinstance(queue, Mapping):
        raise TypeError("Candidate queue must be a mapping.")
    raw = queue.get("tasks")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 200:
        raise ValueError("Request candidate queue tasks invalid.")
    first = raw[0]
    if not isinstance(first, dict) or not isinstance(first.get("candidate_skus"), list):
        raise TypeError("Request candidate shape invalid.")
    candidate_limit = len(first["candidate_skus"])
    expected = build_request_candidate_queue(
        intake, split_manifest, catalog,
        candidates_per_request=candidate_limit, max_requests=len(raw),
    )
    if dict(queue) != expected:
        raise ValueError("Candidate queue was modified or original provenance changed.")
    return True
