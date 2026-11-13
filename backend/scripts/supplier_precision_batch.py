"""Offline source-locked price-precision review batches; never publish supplier offers.

Named human reviewers are self-attested in local JSON (not authenticated here).
Only pinned Ecoplast input is CLI-enabled until other suppliers have audited
source mappings. No implicit cents rounding, ML labels or catalog promotion.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from kpdesk.catalog.supplier_canonical_preview import (
    CanonicalExportRejected,
    ReviewedPrecisionPrice,
    checked_precision_rounding,
    source_row_sha256,
)
from kpdesk.catalog.supplier_staging import (
    StagedSupplierRow,
    SupplierMapping,
    SupplierStaging,
    stage_supplier_prices,
)
from scripts.audit_recent_supplier_candidates import EXPECTED, ROOT

_SOURCE_ID = "avt_el_ecoplast_20260615"
_SCHEMA = "supplier-precision-review-v1"
_FINDINGS = frozenset({
    "approve_rounding", "needs_supplier_confirmation", "reject_source_price",
})
_DECISION_KEYS = frozenset({
    "case_id", "source_row_sha256", "reviewer_id", "reviewed_at",
    "reviewed_source", "finding", "rounding_mode", "rounded_price_rub", "rationale",
})
_MAX_JSON_BYTES = 8 * 1024 * 1024


def load_pinned_ecoplast_staging() -> SupplierStaging:
    """Pin original bytes before parsing; do not fetch, guess or edit source."""
    filename = EXPECTED[_SOURCE_ID][2]
    path = ROOT / filename
    if (
        path.is_symlink() or not path.is_file()
        or path.resolve().parent != ROOT.resolve()
        or path.stat().st_size != EXPECTED[_SOURCE_ID][4]
    ):
        raise ValueError("Pinned Ecoplast original is missing or changed.")
    original = path.read_bytes()
    if hashlib.sha256(original).hexdigest() != EXPECTED[_SOURCE_ID][3]:
        raise ValueError("Pinned Ecoplast original SHA256 mismatch.")
    mapping = SupplierMapping(
        sku=0, name=1, price=4, currency=5,
        sale_unit=2, pieces_per_sale_unit=3,
        header_row=2, sheet="Прайс-лист",
    )
    return stage_supplier_prices(
        filename, original, mapping, formula_policy="quarantine_rows",
    )


def _fingerprint(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_precision_packet(
    staging: SupplierStaging, *, source_id: str,
) -> dict[str, Any]:
    if not isinstance(source_id, str) or not source_id or len(source_id) > 100:
        raise ValueError("Source ID must identify one original supplier.")
    cases: list[dict[str, Any]] = []
    for row in staging.rows:
        if row.source_high_precision_price is None:
            continue
        digest = source_row_sha256(row)
        cases.append({
            "case_id": "precision-" + _fingerprint((
                source_id, staging.sha256, row.line, digest,
            ))[:24],
            "line": row.line,
            "source_row_sha256": digest,
            "source_price_raw": row.source_high_precision_price,
            "source_sku_raw": row.supplier_sku,
            "name_excerpt": row.name[:140],
            "source_unit_raw": row.source_sale_unit,
            "source_pack_size": row.source_pack_size,
            "currency_confirmed_rub": (
                "unconfirmed_or_non_rub_currency" not in row.issues
            ),
            "formula_provenance_unverified": bool(row.formula_columns) or (
                "xls_formula_and_macro_provenance_unverified" in row.issues
            ),
        })
    if len({case["case_id"] for case in cases}) != len(cases):
        raise ValueError("Duplicate precision case ID.")
    return {
        "schema_version": _SCHEMA,
        "source_id": source_id,
        "source_filename": staging.source_filename,
        "source_sha256": staging.sha256,
        "total_staged_rows": len(staging.rows),
        "cases": cases,
        "precision_review_completed": False,
        "supplier_source_reuse_verified": False,
        "current_price_verified": False,
        "canonical_importable": False,
        "published": False,
        "purpose": "local_supplier_price_precision_review_only",
    }


def verify_precision_packet(staging: SupplierStaging, packet: object) -> int:
    if not isinstance(packet, dict):
        raise TypeError("Price precision packet must be an object.")
    source_id = packet.get("source_id")
    if not isinstance(source_id, str):
        raise TypeError("Price precision packet source ID missing.")
    expected = build_precision_packet(staging, source_id=source_id)
    if packet != expected:
        raise ValueError("Price precision source packet mismatch or tampering.")
    return len(expected["cases"])


def build_reviewer_template(
    packet: dict[str, Any], *, start: int = 0, limit: int = 100,
) -> list[dict[str, Any]]:
    if (
        isinstance(start, bool) or not isinstance(start, int) or start < 0
        or isinstance(limit, bool) or not isinstance(limit, int)
        or not 1 <= limit <= 100
    ):
        raise ValueError("Review template start/limit range invalid.")
    cases = packet.get("cases")
    if not isinstance(cases, list):
        raise TypeError("Review template requires a source packet.")
    return [{
        "case_id": case["case_id"],
        "source_row_sha256": case["source_row_sha256"],
        "reviewer_id": "",
        "reviewed_at": "",
        "reviewed_source": None,
        "finding": None,
        "rounding_mode": None,
        "rounded_price_rub": None,
        "rationale": "",
    } for case in cases[start:start + limit]]


def _index(staging: SupplierStaging, packet: dict[str, Any]) -> dict[str, StagedSupplierRow]:
    verify_precision_packet(staging, packet)
    by_line = {row.line: row for row in staging.rows}
    return {
        case["case_id"]: by_line[case["line"]]
        for case in packet["cases"]
    }


def _validated_reviews(
    staging: SupplierStaging, packet: dict[str, Any],
    decisions: list[dict[str, Any]],
) -> tuple[str, dict[str, dict[str, Any]]]:
    cases = _index(staging, packet)
    if not isinstance(decisions, list) or not decisions:
        raise ValueError("At least one actual human review is required.")
    seen: dict[str, dict[str, Any]] = {}
    reviewer_ids: set[str] = set()
    for decision in decisions:
        if not isinstance(decision, dict) or set(decision) != _DECISION_KEYS:
            raise ValueError("Price precision decision has unexpected or missing fields.")
        case_id = decision["case_id"]
        if not isinstance(case_id, str) or case_id not in cases:
            raise ValueError("Unknown precision review case.")
        if case_id in seen:
            raise ValueError("Precision review contains duplicate case ID.")
        seen[case_id] = decision
        source = cases[case_id]
        if decision["source_row_sha256"] != source_row_sha256(source):
            raise ValueError("Price precision review source row SHA256 mismatch.")
        reviewer = decision["reviewer_id"]
        if not isinstance(reviewer, str) or not 1 <= len(reviewer.strip()) <= 120:
            raise ValueError("One identified human reviewer is required.")
        reviewer_ids.add(reviewer.strip())
        if decision["reviewed_source"] != "human_review":
            raise ValueError("Only human review attestations are supported.")
        timestamp = decision["reviewed_at"]
        if not isinstance(timestamp, str):
            raise TypeError("Human review timestamp with timezone required.")
        try:
            parsed_time = datetime.fromisoformat(timestamp)
        except ValueError as error:
            raise ValueError("Invalid human review timestamp.") from error
        if parsed_time.utcoffset() is None:
            raise ValueError("Human review timestamp timezone required.")
        reason = decision["rationale"]
        if (
            not isinstance(reason, str) or not 32 <= len(reason.strip()) <= 500
            or any(ord(ch) < 32 for ch in reason)
        ):
            raise ValueError("Human review rationale must be 32..500 characters.")
        finding = decision["finding"]
        if not isinstance(finding, str) or finding not in _FINDINGS:
            raise ValueError("Unsupported human precision finding.")
        mode = decision["rounding_mode"]
        rounded = decision["rounded_price_rub"]
        if finding != "approve_rounding":
            if mode is not None or rounded is not None:
                raise ValueError("Unresolved precision findings must not invent amounts.")
            continue
        if not isinstance(mode, str) or not isinstance(rounded, str):
            raise TypeError("Approved rounded_price_rub and rounding_mode must be strings.")
        try:
            amount = Decimal(rounded)
        except InvalidOperation as error:
            raise ValueError("Invalid rounded_price_rub decimal.") from error
        try:
            checked_precision_rounding(source, ReviewedPrecisionPrice(
                price_rub=amount,
                rounding_mode=mode,  # type: ignore[arg-type]
                explanation=reason,
            ))
        except CanonicalExportRejected as error:
            raise ValueError(f"Invalid human precision rounding: {error}") from error
    if len(reviewer_ids) != 1:
        raise ValueError("one reviewer must author a single batch.")
    return reviewer_ids.pop(), seen


def validate_precision_batch(
    staging: SupplierStaging, packet: dict[str, Any],
    decisions: list[dict[str, Any]],
) -> dict[str, int | bool]:
    _, checked = _validated_reviews(staging, packet, decisions)
    count = sum(x["finding"] == "approve_rounding" for x in checked.values())
    return {
        "reviewed_count": len(checked),
        "reviewed_rounding_count": count,
        "pending_count": len(checked) - count,
        "human_identity_authenticated": False,
        "current_price_verified": False,
        "canonical_importable": False,
        "published": False,
    }


def _outcome(case: dict[str, Any], finding: dict[str, Any], reviewers: list[str], method: str) -> dict[str, Any]:
    return {
        "case_id": case["case_id"],
        "source_row_sha256": case["source_row_sha256"],
        "source_price_raw": case["source_price_raw"],
        "finding": finding["finding"],
        "rounding_mode": finding["rounding_mode"],
        "rounded_price_rub": finding["rounded_price_rub"],
        "reviewer_ids": reviewers,
        "annotation_method": method,
    }


def adjudicate_precision_batches(
    staging: SupplierStaging, packet: dict[str, Any],
    first: list[dict[str, Any]], second: list[dict[str, Any]],
    *, adjudications: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    reviewer_a, a = _validated_reviews(staging, packet, first)
    reviewer_b, b = _validated_reviews(staging, packet, second)
    if reviewer_a == reviewer_b:
        raise ValueError("Two distinct human reviewers required.")
    if set(a) != set(b):
        raise ValueError("Two independent reviewers must cover the same cases.")
    lookup = {case["case_id"]: case for case in packet["cases"]}
    consensus: list[dict[str, Any]] = []
    conflicts: list[str] = []
    for case_id in sorted(a):
        comparison_a = tuple(a[case_id][key] for key in (
            "finding", "rounding_mode", "rounded_price_rub",
        ))
        comparison_b = tuple(b[case_id][key] for key in (
            "finding", "rounding_mode", "rounded_price_rub",
        ))
        if comparison_a != comparison_b:
            conflicts.append(case_id)
            continue
        consensus.append(_outcome(
            lookup[case_id], a[case_id], [reviewer_a, reviewer_b],
            "two_human_attestations_agree",
        ))
    initial_conflicts = len(conflicts)
    if adjudications is not None:
        if not conflicts:
            if adjudications:
                raise ValueError("No conflicting precision cases for third review.")
        else:
            reviewer_c, c = _validated_reviews(staging, packet, adjudications)
            if reviewer_c in (reviewer_a, reviewer_b):
                raise ValueError("A distinct third human reviewer is required.")
            if set(c) != set(conflicts):
                raise ValueError("Adjudications must cover exact conflicting case IDs.")
            for case_id in sorted(c):
                consensus.append(_outcome(
                    lookup[case_id], c[case_id], [reviewer_a, reviewer_b, reviewer_c],
                    "distinct_third_human_attestation",
                ))
            conflicts = []
    consensus.sort(key=lambda x: x["case_id"])
    approved = sum(item["finding"] == "approve_rounding" for item in consensus)
    return {
        "schema_version": "supplier-precision-consensus-v1",
        "source_sha256": staging.sha256,
        "source_id": packet["source_id"],
        "queue_sha256": _fingerprint(packet),
        "first_batch_sha256": _fingerprint(first),
        "second_batch_sha256": _fingerprint(second),
        "adjudications_sha256": (
            _fingerprint(adjudications) if adjudications is not None else None
        ),
        "total_packet_cases": len(packet["cases"]),
        "reviewed_case_count": len(a),
        "untouched_case_count": len(packet["cases"]) - len(a),
        "agreement_count": len(a) - initial_conflicts,
        "adjudicated_count": initial_conflicts - len(conflicts),
        "conflict_count": len(conflicts),
        "unresolved_case_ids": conflicts,
        "approved_rounding_count": approved,
        "pending_count": len(a) - approved,
        "consensus": consensus,
        "human_identity_authenticated": False,
        "supplier_source_reuse_verified": False,
        "current_price_verified": False,
        "canonical_importable": False,
        "published": False,
    }


def _read_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_JSON_BYTES:
        raise ValueError("Review JSON input missing, redirected or oversized.")
    return json.loads(path.read_text(encoding="utf-8"))


def _create_json(path: Path, data: Any) -> None:
    content = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if len(content.encode("utf-8")) > _MAX_JSON_BYTES:
        raise ValueError("Review JSON output exceeds safe size limit.")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        output.write(content)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pinned offline Ecoplast precision review.")
    sub = parser.add_subparsers(dest="action", required=True)
    build = sub.add_parser("build")
    build.add_argument("--output", type=Path, required=True)
    for name in ("verify", "template", "validate", "consensus"):
        action = sub.add_parser(name)
        action.add_argument("--queue", type=Path, required=True)
        if name == "template":
            action.add_argument("--start", type=int, default=0)
            action.add_argument("--limit", type=int, default=100)
            action.add_argument("--output", type=Path, required=True)
        if name == "validate":
            action.add_argument("--decisions", type=Path, required=True)
        if name == "consensus":
            action.add_argument("--first", type=Path, required=True)
            action.add_argument("--second", type=Path, required=True)
            action.add_argument("--adjudications", type=Path)
            action.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    staging = load_pinned_ecoplast_staging()
    if args.action == "build":
        packet = build_precision_packet(staging, source_id=_SOURCE_ID)
        _create_json(args.output, packet)
        summary: dict[str, Any] = {
            "case_count": len(packet["cases"]),
            "source_sha256": staging.sha256,
            "precision_review_completed": False,
        }
    else:
        packet = _read_json(args.queue)
        verify_precision_packet(staging, packet)
        if args.action == "verify":
            summary = {"case_count": len(packet["cases"]), "integrity_verified": True}
        elif args.action == "template":
            result = build_reviewer_template(
                packet, start=args.start, limit=args.limit,
            )
            _create_json(args.output, result)
            summary = {"template_rows": len(result), "human_reviewed": False}
        elif args.action == "validate":
            decisions = _read_json(args.decisions)
            summary = dict(validate_precision_batch(staging, packet, decisions))
        else:
            first = _read_json(args.first)
            second = _read_json(args.second)
            third = _read_json(args.adjudications) if args.adjudications else None
            consensus = adjudicate_precision_batches(
                staging, packet, first, second, adjudications=third,
            )
            _create_json(args.output, consensus)
            summary = {
                key: value for key, value in consensus.items()
                if key not in ("consensus", "unresolved_case_ids")
            }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
