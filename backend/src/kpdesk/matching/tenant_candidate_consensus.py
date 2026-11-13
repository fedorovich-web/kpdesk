"""Authenticated reviewer agreement analysis: never ML gold by itself."""
from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any, Protocol


class CandidateReviewHistoryReader(Protocol):
    def history(self, queue_sha256: str, *, limit: int, offset: int) -> list[dict[str, Any]]: ...


def calculate_tenant_consensus(
    store: CandidateReviewHistoryReader,
    queue_sha256: str,
    *,
    max_reviews: int = 5000,
) -> dict[str, Any]:
    """Read the entire bounded source-locked queue, never a single partial page.

    Distinct authenticated user UUIDs are mandatory. Unresolved dispositions,
    disagreement, or only one reviewer cannot be accepted as agreement.
    """
    if type(max_reviews) is not int or not 2 <= max_reviews <= 5000:
        raise ValueError("Invalid consensus audit scan limit.")
    rows: list[dict[str, Any]] = []
    for offset in range(0, max_reviews + 1, 100):
        page = store.history(queue_sha256, limit=100, offset=offset)
        if offset + len(page) > max_reviews:
            raise ValueError("Review queue exceeds bounded consensus scan.")
        rows.extend(page)
        if len(page) < 100:
            break
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if not isinstance(row.get("reviewer_user_id"), uuid.UUID):
            raise TypeError("Review history lacks an authenticated reviewer UUID.")
        grouped[str(row["task_sha256"])].append(row)
    agreements: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    incomplete: list[dict[str, Any]] = []
    for task_sha, decisions in sorted(grouped.items()):
        identities = {row["reviewer_user_id"] for row in decisions}
        request_ids = {row["request_id"] for row in decisions}
        if len(identities) != len(decisions) or len(request_ids) != 1:
            raise ValueError("Corrupt or duplicate authenticated review identity.")
        entry: dict[str, Any] = {
            "request_id": decisions[0]["request_id"],
            "task_sha256": task_sha,
            "reviewer_count": len(identities),
            "reviewer_user_ids": sorted(str(user) for user in identities),
        }
        if len(decisions) < 2:
            incomplete.append(entry)
            continue
        fingerprints = {
            (
                tuple(sorted(row["accepted_skus"])),
                tuple(sorted(row["rejected_skus"])),
                tuple(sorted(row["unresolved_skus"])),
                row["no_candidate_match"],
                tuple(row["candidate_skus"]),
                row["source_sha256"],
                row["source_batch_id"],
                row["partition"],
            )
            for row in decisions
        }
        if len(fingerprints) != 1:
            conflicts.append(entry)
        elif decisions[0]["unresolved_skus"]:
            incomplete.append(entry)
        else:
            entry["accepted_skus"] = sorted(decisions[0]["accepted_skus"])
            entry["rejected_skus"] = sorted(decisions[0]["rejected_skus"])
            entry["no_candidate_match_among_proposals"] = decisions[0]["no_candidate_match"]
            agreements.append(entry)
    return {
        "queue_sha256": queue_sha256,
        "source_bound_review_count": len(rows),
        "agreed": agreements,
        "conflicts": conflicts,
        "incomplete": incomplete,
        "distinct_authenticated_reviewers_required": 2,
        "origin_independently_verified": False,
        "rights_verified": False,
        "privacy_review_complete": False,
        "all_catalog_relevance_verified": False,
        "independent_gold": False,
        "training_approved": False,
        "benchmark_approved": False,
    }
