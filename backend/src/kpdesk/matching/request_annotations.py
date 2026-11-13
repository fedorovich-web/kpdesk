"""Offline source-locked human candidate-review evidence, never auto-generated gold.

Reviewer names inside local files are self-asserted, not authenticated sessions.
Rights, personal data, commercial catalog provenance and test-set independence
require separate authorizations before any training or benchmarking.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SHA_PATTERN = r"^[0-9a-f]{64}$"


def _sha(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


class CandidateDecision(BaseModel):
    """Only literal human judgments of displayed candidates, not all catalog SKUs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1, max_length=100)
    task_sha256: str = Field(pattern=_SHA_PATTERN)
    reviewed_source: Literal["human_review"]
    reviewed_by: str = Field(min_length=3, max_length=120)
    reviewed_at: datetime
    rationale: str = Field(min_length=32, max_length=1000)
    accepted_skus: tuple[str, ...] = ()
    rejected_skus: tuple[str, ...] = ()
    unresolved_skus: tuple[str, ...] = ()
    no_candidate_match: bool = Field(strict=True)

    @field_validator("reviewed_by", "rationale")
    @classmethod
    def nonempty_and_printable(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("Human review identity and rationale must be printable.")
        return value

    @field_validator("reviewed_at")
    @classmethod
    def reviewed_timezone(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("Human review needs a timezone-aware timestamp.")
        return value

    @model_validator(mode="after")
    def coherent_judgment(self) -> CandidateDecision:
        parts = (
            self.accepted_skus, self.rejected_skus, self.unresolved_skus,
        )
        if any(len(items) != len(set(items)) for items in parts):
            raise ValueError("Duplicate SKU within reviewer decision.")
        if any(not sku or len(sku) > 100 for part in parts for sku in part):
            raise ValueError("Invalid reviewer SKU.")
        all_skus = [sku for part in parts for sku in part]
        if len(all_skus) != len(set(all_skus)):
            raise ValueError("Conflicting candidate judgments.")
        if self.no_candidate_match and (
            self.accepted_skus or self.unresolved_skus or not self.rejected_skus
        ):
            raise ValueError("No-candidate-match requires every displayed SKU rejected.")
        if not self.no_candidate_match and not (
            self.accepted_skus or self.unresolved_skus
        ):
            raise ValueError("No-positive decision must use explicit no-candidate-match.")
        return self


def _tasks(queue: Mapping[str, object]) -> dict[str, dict[str, Any]]:
    if (
        queue.get("schema_version") != "request-candidate-review-v1"
        or queue.get("source_excerpts_verified") is not True
        or any(queue.get(key) is not False for key in (
            "origin_independently_verified", "rights_verified",
            "privacy_review_complete", "catalog_reuse_rights_verified",
            "all_relevance_labels_reviewed", "independent_gold",
            "training_approved", "benchmark_approved",
        ))
    ):
        raise ValueError("Candidate packet has unverified or elevated permissions.")
    raw = queue.get("tasks")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 200:
        raise ValueError("Invalid source-bound candidate packet.")
    result: dict[str, dict[str, Any]] = {}
    for task in raw:
        if not isinstance(task, dict):
            raise TypeError("Candidate task must be an object.")
        request_id = task.get("request_id")
        candidate_skus = task.get("candidate_skus")
        if (
            not isinstance(request_id, str) or request_id in result
            or task.get("partition") not in ("train", "validation")
            or task.get("candidate_origin") != "lexical_proposals_not_labels"
            or task.get("review_state") != "unreviewed"
            or any(task.get(key) is not None for key in (
                "reviewed_positive_skus", "reviewed_negative_skus", "reviewed_no_match",
            ))
            or not isinstance(candidate_skus, list)
            or not 2 <= len(candidate_skus) <= 20
            or not all(isinstance(s, str) for s in candidate_skus)
            or len(candidate_skus) != len(set(candidate_skus))
        ):
            raise ValueError("Candidate task identity, split or label provenance invalid.")
        result[request_id] = task
    return result


def build_review_template(queue: Mapping[str, object]) -> list[dict[str, object]]:
    """Do not pre-fill human labels; even no-candidate-match is unset."""
    return [
        {
            "request_id": request_id, "task_sha256": _sha(task),
            "reviewed_source": None, "reviewed_by": "", "reviewed_at": "",
            "rationale": "", "accepted_skus": [], "rejected_skus": [],
            "unresolved_skus": [], "no_candidate_match": None,
        }
        for request_id, task in sorted(_tasks(queue).items())
    ]


def validate_review_batch(
    queue: Mapping[str, object],
    raw_decisions: Sequence[Mapping[str, object]],
) -> tuple[str, dict[str, CandidateDecision]]:
    """Check every proposed SKU has exactly one literal human disposition.

    Partial batches are supported, but a batch must have one reviewer and
    cannot include locked test queries or arbitrary catalog SKU values.
    """
    tasks = _tasks(queue)
    if not 1 <= len(raw_decisions) <= len(tasks):
        raise ValueError("Human candidate review batch empty or excessive.")
    result: dict[str, CandidateDecision] = {}
    reviewers: set[str] = set()
    for raw in raw_decisions:
        decision = CandidateDecision.model_validate(raw)
        task = tasks.get(decision.request_id)
        if task is None or decision.request_id in result:
            raise ValueError("Unknown, held-out or duplicate request in review batch.")
        if decision.task_sha256 != _sha(task):
            raise ValueError("Source-locked task SHA256 mismatch.")
        candidate_skus = task["candidate_skus"]
        all_skus = (
            *decision.accepted_skus, *decision.rejected_skus, *decision.unresolved_skus,
        )
        if set(all_skus) != set(candidate_skus):
            raise ValueError("Every displayed candidate requires an explicit judgment.")
        reviewers.add(decision.reviewed_by.strip())
        result[decision.request_id] = decision
    if len(reviewers) != 1:
        raise ValueError("One human reviewer per independent batch.")
    return reviewers.pop(), result


def compare_review_batches(
    queue: Mapping[str, object],
    first: Sequence[Mapping[str, object]],
    second: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Compare independent human opinions without minting production gold."""
    reviewer_a, decisions_a = validate_review_batch(queue, first)
    reviewer_b, decisions_b = validate_review_batch(queue, second)
    if reviewer_a == reviewer_b:
        raise ValueError("Two distinct human reviewers are required.")
    if set(decisions_a) != set(decisions_b):
        raise ValueError("Reviewer batches must cover identical source-bound requests.")
    agreements: list[dict[str, object]] = []
    conflicts: list[str] = []
    unresolved: list[str] = []
    for request_id in sorted(decisions_a):
        a, b = decisions_a[request_id], decisions_b[request_id]
        a_labels = (
            frozenset(a.accepted_skus), frozenset(a.rejected_skus),
            frozenset(a.unresolved_skus), a.no_candidate_match,
        )
        b_labels = (
            frozenset(b.accepted_skus), frozenset(b.rejected_skus),
            frozenset(b.unresolved_skus), b.no_candidate_match,
        )
        if a_labels != b_labels:
            conflicts.append(request_id)
        elif a.unresolved_skus:
            unresolved.append(request_id)
        else:
            agreements.append({
                "request_id": request_id,
                "task_sha256": a.task_sha256,
                "accepted_skus": sorted(a.accepted_skus),
                "rejected_skus": sorted(a.rejected_skus),
                "no_candidate_match": a.no_candidate_match,
                "reviewed_by": [reviewer_a, reviewer_b],
                "agreement_kind": "two_self_attested_human_reviews",
            })
    return {
        "schema_version": "request-candidate-review-consensus-v1",
        "queue_sha256": _sha(queue),
        "first_batch_sha256": _sha(first),
        "second_batch_sha256": _sha(second),
        "reviewed_request_count": len(decisions_a),
        "agreement_count": len(agreements),
        "conflicting_request_ids": conflicts,
        "unresolved_request_ids": unresolved,
        "agreements": agreements,
        "reviewer_authentication_verified": False,
        "origin_independently_verified": False,
        "rights_verified": False,
        "privacy_review_complete": False,
        "all_catalog_relevance_verified": False,
        "independent_gold": False,
        "training_approved": False,
        "benchmark_approved": False,
    }
