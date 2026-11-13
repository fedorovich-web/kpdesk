"""Double-review consensus and source-locked annotation provenance tests."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from kpdesk.matching.review_queue import (
    adjudicate_review_batches,
    build_review_queue,
    verify_review_queue,
)

CORPUS = Path(__file__).resolve().parents[2] / "ml/data/real/public_supplier_prices"


@pytest.fixture(scope="module")
def queue() -> dict[str, Any]:
    return build_review_queue(CORPUS, per_source=1, candidate_limit=3)


def _decision(task: dict[str, Any], reviewer: str, *, no_match: bool = False) -> dict[str, Any]:
    ids = [item["product_id"] for item in task["candidates"]]
    accepted = [] if no_match else [task["seed_product_id"]]
    return {
        "task_id": task["task_id"],
        "request_text": task["request_text"],
        "query_origin": task["query_origin"],
        "reviewed_source": "human_review",
        "reviewed_by": reviewer,
        "reviewed_at": "2026-10-09T11:00:00+02:00",
        "accepted_product_ids": accepted,
        "rejected_product_ids": [product for product in ids if product not in accepted],
        "no_match": no_match,
        "rationale": "Manually compared catalog description and requested properties.",
    }


def test_full_queue_is_rebuilt_from_originals_and_detects_tampering(queue: dict[str, Any]) -> None:
    result = verify_review_queue(CORPUS, queue)
    assert result["tasks"] == 6
    assert result["verified_originals"] == 6
    forged = deepcopy(queue)
    forged["tasks"][0]["candidates"][0]["name"] = "different title"
    with pytest.raises(ValueError, match="source|tamper|mismatch"):
        verify_review_queue(CORPUS, forged)
    forged = deepcopy(queue)
    forged["source_sha256"]["svih_lighting"] = "0" * 64
    with pytest.raises(ValueError, match="source|tamper|mismatch"):
        verify_review_queue(CORPUS, forged)


def test_two_reviewers_consensus_never_claims_independent_real_request_gold(
    queue: dict[str, Any],
) -> None:
    one = [_decision(task, "reviewer-A") for task in queue["tasks"]]
    two = [_decision(task, "reviewer-B") for task in queue["tasks"]]
    result = adjudicate_review_batches(CORPUS, queue, one, two)
    assert result["reviewed_task_count"] == 6
    assert result["agreement_count"] == 6
    assert result["conflict_count"] == 0
    assert result["unresolved_task_ids"] == []
    assert result["independent_real_request_gold"] is False
    assert result["training_approved"] is False
    assert result["consensus"][0]["query_origin"] == "synthetic_from_real_catalog"
    assert all("accepted_product_ids" in item for item in result["consensus"])


def test_conflict_is_explicit_and_third_reviewer_can_resolve(queue: dict[str, Any]) -> None:
    one = [_decision(task, "reviewer-A") for task in queue["tasks"]]
    two = [_decision(task, "reviewer-B") for task in queue["tasks"]]
    two[0] = _decision(queue["tasks"][0], "reviewer-B", no_match=True)
    pending = adjudicate_review_batches(CORPUS, queue, one, two)
    assert pending["agreement_count"] == 5
    assert pending["conflict_count"] == 1
    assert pending["unresolved_task_ids"] == [queue["tasks"][0]["task_id"]]
    assert len(pending["consensus"]) == 5
    deciding = [_decision(queue["tasks"][0], "reviewer-C")]
    resolved = adjudicate_review_batches(
        CORPUS, queue, one, two, adjudications=deciding,
    )
    assert resolved["conflict_count"] == 0
    assert resolved["adjudicated_count"] == 1
    assert len(resolved["consensus"]) == 6
    assert resolved["training_approved"] is False


def test_reviewers_must_be_distinct_humans_not_models(queue: dict[str, Any]) -> None:
    one = [_decision(task, "reviewer-A") for task in queue["tasks"]]
    with pytest.raises(ValueError, match="distinct"):
        adjudicate_review_batches(CORPUS, queue, one, one)
    two = [_decision(task, "reviewer-B") for task in queue["tasks"]]
    two[0]["reviewed_source"] = "model_prediction"
    with pytest.raises(ValueError, match="human"):
        adjudicate_review_batches(CORPUS, queue, one, two)


def test_only_identical_task_sets_may_be_compared(queue: dict[str, Any]) -> None:
    one = [_decision(task, "reviewer-A") for task in queue["tasks"]]
    two = [_decision(task, "reviewer-B") for task in queue["tasks"][:-1]]
    with pytest.raises(ValueError, match="same tasks"):
        adjudicate_review_batches(CORPUS, queue, one, two)


def test_adjudicator_must_not_be_original_reviewer(queue: dict[str, Any]) -> None:
    one = [_decision(task, "reviewer-A") for task in queue["tasks"]]
    two = [_decision(task, "reviewer-B") for task in queue["tasks"]]
    two[0] = _decision(queue["tasks"][0], "reviewer-B", no_match=True)
    deciding = [_decision(queue["tasks"][0], "reviewer-A")]
    with pytest.raises(ValueError, match="third|distinct"):
        adjudicate_review_batches(CORPUS, queue, one, two, adjudications=deciding)


def test_reviewer_packet_hides_seed_and_gold_status(queue: dict[str, Any]) -> None:
    from kpdesk.matching.review_queue import build_blind_reviewer_packet

    blinded = build_blind_reviewer_packet(CORPUS, queue)
    assert blinded["schema_version"] == "blind-review-packet-v1"
    assert blinded["human_labels"] is False
    assert blinded["source_count"] == 6
    assert len(blinded["tasks"]) == 6
    for task in blinded["tasks"]:
        assert task["task_id"].startswith("case-")
        assert "seed_product_id" not in task
        assert "query_group_id" not in task
        assert "relevant_skus" not in task
        assert len(task["candidates"]) == 3
        for candidate in task["candidates"]:
            assert set(candidate) == {"product_id", "name", "supplier_sku", "source_row"}
            assert "reviewed_label" not in candidate
    assert build_blind_reviewer_packet(CORPUS, queue) == blinded


def test_reviewer_packet_requires_intact_source_queue(queue: dict[str, Any]) -> None:
    from kpdesk.matching.review_queue import build_blind_reviewer_packet

    forged = deepcopy(queue)
    forged["tasks"][0]["candidates"][0]["name"] = "fabricated"
    with pytest.raises(ValueError, match="mismatch|source|tamper"):
        build_blind_reviewer_packet(CORPUS, forged)


def test_opaque_packet_id_is_resolved_for_validated_human_decision(
    queue: dict[str, Any],
) -> None:
    from kpdesk.matching.review_queue import (
        build_blind_reviewer_packet,
        validate_reviewed_decisions,
    )

    packet = build_blind_reviewer_packet(CORPUS, queue)
    task = queue["tasks"][0]
    first = _decision(task, "reviewer-A")
    second = _decision(task, "reviewer-B")
    first["task_id"] = packet["tasks"][0]["task_id"]
    second["task_id"] = packet["tasks"][0]["task_id"]
    checked = validate_reviewed_decisions(queue, [first])
    assert checked["reviewed_count"] == 1
    result = adjudicate_review_batches(CORPUS, queue, [first], [second])
    assert result["agreement_count"] == 1
    assert result["consensus"][0]["task_id"].startswith("case-")
    duplicate = _decision(task, "reviewer-A")
    with pytest.raises(ValueError, match="duplicate"):
        validate_reviewed_decisions(queue, [first, duplicate])
