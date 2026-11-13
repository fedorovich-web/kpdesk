"""Human-label review gates for genuine supplier price candidate queues."""

from pathlib import Path

import pytest

from kpdesk.matching.review_queue import build_review_queue, validate_reviewed_decisions

CORPUS = Path(__file__).resolve().parents[2] / "ml/data/real/public_supplier_prices"


def test_build_unreviewed_queue_with_hard_negative_candidates() -> None:
    queue = build_review_queue(CORPUS, per_source=4, candidate_limit=5)
    assert len(queue["tasks"]) == 24
    assert len(queue["source_sha256"]) == 6
    for task in queue["tasks"]:
        assert task["query_origin"] == "synthetic_from_real_catalog"
        assert task["review_state"] == "unreviewed"
        assert task["request_text"].startswith("Нужно ")
        assert len(task["candidates"]) >= 2
        assert all(c["reviewed_label"] == "unknown" for c in task["candidates"])
    assert queue == build_review_queue(CORPUS, per_source=4, candidate_limit=5)


def test_pseudo_queries_do_not_become_gold() -> None:
    queue = build_review_queue(CORPUS, per_source=1, candidate_limit=4)
    assert queue["training_approved"] is False
    assert queue["independent_gold"] is False
    task = queue["tasks"][0]
    ids = [c["product_id"] for c in task["candidates"]]
    decision = {"task_id": task["task_id"], "request_text": task["request_text"],
                "query_origin": task["query_origin"], "reviewed_by": "reviewer-01",
                "reviewed_at": "2026-10-09T10:00:00+02:00",
                "accepted_product_ids": [ids[0]], "rejected_product_ids": ids[1:],
                "no_match": False, "rationale": "checked"}
    result = validate_reviewed_decisions(queue, [decision])
    assert result["reviewed_count"] == 1
    assert result["hard_negative_count"] == len(ids) - 1
    assert result["independent_gold_count"] == 0
    assert result["training_approved"] is False
    with pytest.raises(ValueError):
        validate_reviewed_decisions(queue, [{**decision, "rejected_product_ids": []}])
    with pytest.raises(ValueError):
        validate_reviewed_decisions(queue, [{**decision, "query_origin": "real_customer_request"}])
    with pytest.raises(ValueError):
        validate_reviewed_decisions(queue, [{**decision, "accepted_product_ids": ["invented"]}])
    with pytest.raises(ValueError):
        validate_reviewed_decisions(queue, [decision, decision])
