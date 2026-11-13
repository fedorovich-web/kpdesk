"""Source-bound human-attested supplier quality decisions, never catalog approvals.

This protocol records *findings*, not authorization for prices or ML training.
Named human reviewers are self-attested here, not externally authenticated.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from scripts.prepare_supplier_quality_triage import verify_supplier_quality_triage

FINDINGS_BY_ISSUE: dict[str, frozenset[str]] = {
    "ecoplast_duplicate_sku": frozenset({
        "same_product", "distinct_products", "needs_supplier_confirmation", "unresolved",
    }),
    "ecoplast_invalid_package": frozenset({
        "fractional_quantity_confirmed", "invalid_quantity",
        "needs_supplier_confirmation", "unresolved",
    }),
    "shveigid_missing_article": frozenset({
        "no_article_in_source", "needs_supplier_confirmation", "unresolved",
    }),
    "shveigid_duplicate_article": frozenset({
        "same_product", "distinct_products", "needs_supplier_confirmation", "unresolved",
    }),
    "etk_cached_formula_price": frozenset({
        "formula_cache_untrusted", "requires_recalculation", "unresolved",
    }),
    "technoas_currency_unverified": frozenset({
        "currency_not_stated_in_source", "needs_supplier_confirmation", "unresolved",
    }),
}
REVIEW_KEYS = frozenset({
    "case_id", "original_sha256", "reviewed_source", "reviewed_by",
    "reviewed_at", "finding", "rationale",
})
OPEN_FINDINGS = frozenset({
    "unresolved", "needs_supplier_confirmation", "requires_recalculation",
    "formula_cache_untrusted", "currency_not_stated_in_source",
})


def _cases(queue: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if (
        queue.get("schema_version") != "supplier-quality-triage-v1"
        or queue.get("training_authorized") is not False
        or queue.get("canonical_importable") is not False
        or queue.get("reuse_rights_reviewed") is not False
        or queue.get("current_price_verified") is not False
        or queue.get("human_reviewed") is not False
    ):
        raise ValueError("Supplier quality queue must be unapproved, unreviewed source triage.")
    original_cases = queue.get("cases")
    source_hashes = queue.get("source_sha256")
    if not isinstance(original_cases, list) or not isinstance(source_hashes, dict):
        raise TypeError("Supplier source quality queue is malformed.")
    result: dict[str, dict[str, Any]] = {}
    for case in original_cases:
        if not isinstance(case, dict):
            raise TypeError("Supplier source quality case must be an object.")
        case_id = case.get("case_id")
        issue = case.get("issue")
        if (
            not isinstance(case_id, str)
            or case_id in result
            or not isinstance(issue, str)
            or issue not in FINDINGS_BY_ISSUE
            or not isinstance(case.get("source_id"), str)
            or not isinstance(case.get("original_sha256"), str)
            or len(case["original_sha256"]) != 64
            or source_hashes.get(case["source_id"]) != case["original_sha256"]
        ):
            raise ValueError("Supplier source quality case identity or hash mismatch.")
        result[case_id] = case
    if not result:
        raise ValueError("Supplier source quality queue has no cases.")
    return result


def validate_supplier_quality_decisions(
    queue: dict[str, Any], decisions: list[dict[str, Any]],
) -> dict[str, int | bool]:
    """Validate each typed finding against a source-locked case; accept partial batches."""
    cases = _cases(queue)
    seen: set[str] = set()
    for record in decisions:
        if not isinstance(record, dict):
            raise TypeError("Human quality review must be an object.")
        if set(record) != REVIEW_KEYS:
            raise ValueError("Quality decision has unrecognized or unexpected fields.")
        case_id = record.get("case_id")
        if not isinstance(case_id, str) or case_id not in cases:
            raise ValueError("Quality decision references unknown source case.")
        if case_id in seen:
            raise ValueError("Quality decision contains duplicate case ID.")
        seen.add(case_id)
        case = cases[case_id]
        if record.get("original_sha256") != case["original_sha256"]:
            raise ValueError("Quality decision source hash mismatch.")
        if record.get("reviewed_source") != "human_review":
            raise ValueError("Only self-attested human review is supported.")
        reviewer = record.get("reviewed_by")
        if not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 120:
            raise ValueError("Human reviewer ID must be provided.")
        rationale = record.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 2000:
            raise ValueError("Human rationale is required and must be bounded.")
        finding = record.get("finding")
        if not isinstance(finding, str) or finding not in FINDINGS_BY_ISSUE[case["issue"]]:
            raise ValueError("Issue-specific quality finding is not recognized.")
        raw_timestamp = record.get("reviewed_at")
        if not isinstance(raw_timestamp, str):
            raise TypeError("Human review timestamp with timezone is required.")
        try:
            timestamp = datetime.fromisoformat(raw_timestamp)
        except ValueError as error:
            raise ValueError("Invalid quality review timestamp.") from error
        if timestamp.utcoffset() is None:
            raise ValueError("Quality review timestamp must include timezone.")
    return {
        "reviewed_case_count": len(seen),
        "human_attested_not_authenticated": True,
        "human_review_authenticated": False,
        "canonical_importable": False,
        "training_authorized": False,
    }


def build_supplier_quality_review_template(queue: dict[str, Any]) -> list[dict[str, Any]]:
    """Create unfilled human-only input records, never fabricated attestations."""
    cases = _cases(queue)
    return [{
        "case_id": case_id,
        "original_sha256": case["original_sha256"],
        "reviewed_source": None,
        "reviewed_by": "",
        "reviewed_at": "",
        "finding": None,
        "rationale": "",
    } for case_id, case in sorted(cases.items())]


def _batch(
    queue: dict[str, Any], decisions: list[dict[str, Any]],
) -> tuple[str, dict[str, dict[str, Any]]]:
    if not decisions:
        raise ValueError("Quality reviewer batch cannot be empty.")
    validate_supplier_quality_decisions(queue, decisions)
    reviewers = {record["reviewed_by"].strip() for record in decisions}
    if len(reviewers) != 1:
        raise ValueError("One reviewer must author one quality batch.")
    return reviewers.pop(), {record["case_id"]: record for record in decisions}


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def adjudicate_supplier_quality_batches(
    root: Path,
    queue: dict[str, Any],
    first: list[dict[str, Any]],
    second: list[dict[str, Any]],
    *,
    adjudications: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Require two independent complete batches; arbitrate only actual conflicts."""
    # Reject malformed reviewer batches before re-reading large original XLSX
    # files. A successful consensus still verifies every source before return.
    cases = _cases(queue)
    reviewer_a, decisions_a = _batch(queue, first)
    reviewer_b, decisions_b = _batch(queue, second)
    if reviewer_a == reviewer_b:
        raise ValueError("Two distinct human reviewers are required.")
    if set(decisions_a) != set(decisions_b) or set(decisions_a) != set(cases):
        raise ValueError("Both reviewers must cover the same cases, including all queue cases.")

    conflicts: list[str] = []
    consensus: list[dict[str, Any]] = []
    for case_id in sorted(cases):
        a = decisions_a[case_id]
        b = decisions_b[case_id]
        if a["finding"] != b["finding"]:
            conflicts.append(case_id)
            continue
        consensus.append({
            "case_id": case_id,
            "source_id": cases[case_id]["source_id"],
            "original_sha256": cases[case_id]["original_sha256"],
            "finding": a["finding"],
            "annotation_method": "two_human_attestations_agree",
            "reviewer_ids": [reviewer_a, reviewer_b],
        })
    initial_conflicts = len(conflicts)
    if adjudications is not None:
        if not conflicts and adjudications:
            raise ValueError("Third-party review only applies to exact conflicting cases.")
        if conflicts:
            reviewer_c, decisions_c = _batch(queue, adjudications)
            if reviewer_c in (reviewer_a, reviewer_b):
                raise ValueError("A distinct third reviewer is required for conflicts.")
            if set(decisions_c) != set(conflicts):
                raise ValueError("Third reviewer must adjudicate exact conflicting case IDs.")
            for case_id in sorted(decisions_c):
                consensus.append({
                    "case_id": case_id,
                    "source_id": cases[case_id]["source_id"],
                    "original_sha256": cases[case_id]["original_sha256"],
                    "finding": decisions_c[case_id]["finding"],
                    "annotation_method": "distinct_third_human_attestation",
                    "reviewer_ids": [reviewer_a, reviewer_b, reviewer_c],
                })
            conflicts = []
    verify_supplier_quality_triage(root, queue)
    consensus.sort(key=lambda item: item["case_id"])
    resolved_count = sum(item["finding"] not in OPEN_FINDINGS for item in consensus)
    return {
        "schema_version": "supplier-quality-consensus-v1",
        "queue_sha256": _digest(queue),
        "first_batch_sha256": _digest(first),
        "second_batch_sha256": _digest(second),
        "adjudications_sha256": _digest(adjudications) if adjudications is not None else None,
        "reviewed_case_count": len(cases),
        "agreement_count": len(cases) - initial_conflicts,
        "adjudicated_count": initial_conflicts - len(conflicts),
        "conflict_count": len(conflicts),
        "unresolved_case_ids": conflicts,
        "resolved_case_count": resolved_count,
        "pending_case_count": len(cases) - resolved_count,
        "consensus": consensus,
        "human_review_authenticated": False,
        "source_reuse_authorized": False,
        "current_price_verified": False,
        "canonical_importable": False,
        "training_authorized": False,
    }
