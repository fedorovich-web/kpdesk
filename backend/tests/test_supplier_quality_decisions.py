"""Supplier quality decisions remain source-bound, human-attested, and non-promoting."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from scripts.audit_recent_supplier_candidates import ROOT
from scripts.prepare_supplier_quality_triage import build_supplier_quality_triage
from scripts.supplier_quality_decisions import (
    adjudicate_supplier_quality_batches,
    validate_supplier_quality_decisions,
)


@pytest.fixture(scope="module")
def queue() -> dict[str, Any]:
    return build_supplier_quality_triage(ROOT, per_issue=1)


def _reviews(
    queue: dict[str, Any], reviewer: str,
    *, finding: str = "unresolved",
) -> list[dict[str, Any]]:
    return [{
        "case_id": case["case_id"],
        "original_sha256": case["original_sha256"],
        "reviewed_source": "human_review",
        "reviewed_by": reviewer,
        "reviewed_at": "2026-10-21T19:55:00+02:00",
        "finding": finding,
        "rationale": "Source row inspected; evidence still inconclusive.",
    } for case in queue["cases"]]


def test_bounded_human_review_schema_rejects_forged_or_incomplete_decisions(
    queue: dict[str, Any],
) -> None:
    reviews = _reviews(queue, "reviewer-one")
    result = validate_supplier_quality_decisions(queue, reviews)
    assert result["reviewed_case_count"] == 6
    assert result["training_authorized"] is False
    assert result["canonical_importable"] is False
    assert result["human_attested_not_authenticated"] is True
    mutations = [
        ("case_id", "quality-nonexistent", "unknown"),
        ("original_sha256", "0" * 64, "source|hash"),
        ("reviewed_source", "model_prediction", "human"),
        ("reviewed_by", "", "reviewer"),
        ("reviewed_at", "2026-10-21T19:55:00", "timezone"),
        ("finding", "auto_import", "finding"),
        ("rationale", "", "rationale"),
    ]
    for key, bad, message in mutations:
        altered = deepcopy(reviews)
        altered[0][key] = bad
        with pytest.raises(ValueError, match=message):
            validate_supplier_quality_decisions(queue, altered)
    with pytest.raises(ValueError, match="duplicate"):
        validate_supplier_quality_decisions(queue, [*reviews, reviews[0]])
    with pytest.raises(ValueError, match="unrecognized|unexpected"):
        validate_supplier_quality_decisions(queue, [{**reviews[0], "training_approved": True}])
    with pytest.raises(ValueError, match="finding"):
        altered = deepcopy(reviews)
        altered[0]["finding"] = "currency_not_stated_in_source"
        validate_supplier_quality_decisions(queue, altered)


def test_agreement_on_unresolved_does_not_count_as_quality_resolution(
    queue: dict[str, Any],
) -> None:
    first = _reviews(queue, "reviewer-one")
    second = _reviews(queue, "reviewer-two")
    result = adjudicate_supplier_quality_batches(ROOT, queue, first, second)
    assert result["schema_version"] == "supplier-quality-consensus-v1"
    assert result["reviewed_case_count"] == 6
    assert result["agreement_count"] == 6
    assert result["conflict_count"] == 0
    assert result["resolved_case_count"] == 0
    assert result["pending_case_count"] == 6
    assert len(result["consensus"]) == 6
    assert result["training_authorized"] is False
    assert result["canonical_importable"] is False
    assert result["human_review_authenticated"] is False


def test_conflict_requires_distinct_third_reviewer_and_preserves_originals(
    queue: dict[str, Any],
) -> None:
    first = _reviews(queue, "reviewer-one")
    second = _reviews(queue, "reviewer-two")
    item = next(c for c in queue["cases"] if c["issue"] == "ecoplast_duplicate_sku")
    index = next(i for i, d in enumerate(second) if d["case_id"] == item["case_id"])
    first[index]["finding"] = "same_product"
    second[index]["finding"] = "distinct_products"
    pending = adjudicate_supplier_quality_batches(ROOT, queue, first, second)
    assert pending["conflict_count"] == 1
    assert pending["unresolved_case_ids"] == [item["case_id"]]
    assert pending["resolved_case_count"] == 0
    assert pending["pending_case_count"] == 6
    with pytest.raises(ValueError, match="distinct"):
        adjudicate_supplier_quality_batches(
            ROOT, queue, first, second, adjudications=[first[index]],
        )
    with pytest.raises(ValueError, match="exact"):
        adjudicate_supplier_quality_batches(
            ROOT, queue, first, second,
            adjudications=_reviews(queue, "reviewer-three"),
        )
    third = _reviews(queue, "reviewer-three")[index]
    third["finding"] = "distinct_products"
    resolved = adjudicate_supplier_quality_batches(
        ROOT, queue, first, second, adjudications=[third],
    )
    assert resolved["conflict_count"] == 0
    assert resolved["adjudicated_count"] == 1
    assert resolved["resolved_case_count"] == 1
    assert resolved["pending_case_count"] == 5
    assert resolved["consensus"][0]["original_sha256"] in queue["source_sha256"].values()
    assert resolved["canonical_importable"] is False


def test_batches_require_exact_case_set_and_two_independent_reviewers(
    queue: dict[str, Any],
) -> None:
    first = _reviews(queue, "reviewer-one")
    with pytest.raises(ValueError, match="distinct"):
        adjudicate_supplier_quality_batches(ROOT, queue, first, deepcopy(first))
    with pytest.raises(ValueError, match="same cases"):
        adjudicate_supplier_quality_batches(
            ROOT, queue, first, _reviews(queue, "reviewer-two")[:-1],
        )
    mixed = _reviews(queue, "reviewer-two")
    mixed[0]["reviewed_by"] = "reviewer-four"
    with pytest.raises(ValueError, match="[Oo]ne reviewer"):
        adjudicate_supplier_quality_batches(ROOT, queue, first, mixed)


def test_invalid_review_batches_fail_before_expensive_supplier_file_audit(
    queue: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import supplier_quality_decisions as decisions_module

    def unexpected_audit(_root: Path, _queue: dict[str, Any]) -> None:
        raise AssertionError("Original files must not be rescanned for invalid review batches.")

    monkeypatch.setattr(decisions_module, "verify_supplier_quality_triage", unexpected_audit)
    first = _reviews(queue, "reviewer-one")
    with pytest.raises(ValueError, match="distinct"):
        adjudicate_supplier_quality_batches(ROOT, queue, first, deepcopy(first))
    with pytest.raises(ValueError, match="same cases"):
        adjudicate_supplier_quality_batches(
            ROOT, queue, first, _reviews(queue, "reviewer-two")[:-1],
        )
    bad = _reviews(queue, "reviewer-two")
    bad[0]["reviewed_by"] = "reviewer-three"
    with pytest.raises(ValueError, match="[Oo]ne reviewer"):
        adjudicate_supplier_quality_batches(ROOT, queue, first, bad)
    with pytest.raises(AssertionError, match="must not be rescanned"):
        adjudicate_supplier_quality_batches(
            ROOT, queue, first, _reviews(queue, "reviewer-two"),
        )


def test_cli_validates_attestations_and_writes_create_only_consensus(
    tmp_path: Path, queue: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.prepare_supplier_quality_triage import main

    packet_file = tmp_path / "source-queue.json"
    reviewer_one = tmp_path / "first.json"
    reviewer_two = tmp_path / "second.json"
    consensus_file = tmp_path / "consensus.json"
    packet_file.write_text(json.dumps(queue, ensure_ascii=False), encoding="utf-8")
    reviewer_one.write_text(json.dumps(_reviews(queue, "reviewer-one")), encoding="utf-8")
    reviewer_two.write_text(json.dumps(_reviews(queue, "reviewer-two")), encoding="utf-8")

    assert main([
        "validate", "--queue", str(packet_file), "--decisions", str(reviewer_one),
    ]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["reviewed_case_count"] == 6
    assert report["human_review_authenticated"] is False

    assert main([
        "consensus", "--queue", str(packet_file),
        "--first", str(reviewer_one), "--second", str(reviewer_two),
        "--output", str(consensus_file),
    ]) == 0
    capsys.readouterr()
    stored = json.loads(consensus_file.read_text(encoding="utf-8"))
    assert stored["reviewed_case_count"] == 6
    assert stored["pending_case_count"] == 6
    assert stored["canonical_importable"] is False
    with pytest.raises(FileExistsError):
        main([
            "consensus", "--queue", str(packet_file),
            "--first", str(reviewer_one), "--second", str(reviewer_two),
            "--output", str(consensus_file),
        ])


def test_cli_reviewer_template_is_unfilled_and_create_only(
    tmp_path: Path, queue: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.prepare_supplier_quality_triage import main

    packet_path = tmp_path / "packet.json"
    template_path = tmp_path / "reviewer-template.json"
    packet_path.write_text(json.dumps(queue, ensure_ascii=False), encoding="utf-8")
    assert main([
        "template", "--queue", str(packet_path), "--output", str(template_path),
    ]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["case_count"] == 6
    template = json.loads(template_path.read_text(encoding="utf-8"))
    assert len(template) == 6
    assert all(item["finding"] is None and item["reviewed_by"] == "" and item["reviewed_source"] is None for item in template)
    assert all(item["original_sha256"] == queue["source_sha256"][
        next(case["source_id"] for case in queue["cases"] if case["case_id"] == item["case_id"])
    ] for item in template)
    with pytest.raises(ValueError, match="human"):
        validate_supplier_quality_decisions(queue, template)
    with pytest.raises(FileExistsError):
        main(["template", "--queue", str(packet_path), "--output", str(template_path)])


def test_source_hash_and_source_id_are_mandatory_even_in_direct_validation(
    queue: dict[str, Any],
) -> None:
    forged = deepcopy(queue)
    forged["cases"][0]["source_id"] = None
    forged["cases"][0]["original_sha256"] = None
    decisions = _reviews(forged, "reviewer-one")
    with pytest.raises(ValueError, match="source|hash"):
        validate_supplier_quality_decisions(forged, decisions)
