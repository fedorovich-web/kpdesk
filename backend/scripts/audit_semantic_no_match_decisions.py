"""Compare frozen SKU suggestions with real deterministic quote-draft decisions.

This deliberately uses an existing synthetic, hand-authored no-match holdout:
its gold labels are NOT independently verified supplier/customer evidence.
No threshold adjustment, ML training, real DB access or paid LLM calls.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from kpdesk.config import PROJECT_ROOT
from kpdesk.matching.cli import load_dataset
from kpdesk.matching.decision_audit import audit_auto_selection
from kpdesk.matching.normalize import normalize_text
from kpdesk.quotes.auto_draft import build_auto_draft
from scripts.evaluate_no_match_holdout import (
    _HOLDOUT,
    _SOURCE,
    HoldoutDocument,
    benchmark_holdout,
    load_holdout,
)

_ALLOWED_REASONS = frozenset((
    "absent_category", "incompatible_profile", "semantic_absence",
))


def audit_no_match_draft_safety() -> dict[str, Any]:
    """Return shared-case suggestion/draft evidence; never infer production quality."""
    training = load_dataset(_SOURCE)
    if training.provenance_kind != "synthetic_example" or training.independent_labels:
        raise ValueError("No-match audit expects only synthetic provenance")
    cases = load_holdout(_HOLDOUT, training.queries, training.products)
    doc = HoldoutDocument.model_validate_json(_HOLDOUT.read_bytes())
    by_id = {case.id: case for case in doc.cases}
    if len(cases) != 22 or len(by_id) != len(cases):
        raise ValueError("Held-out synthetic case identity has changed")
    if {row.query_id for row in cases} != set(by_id):
        raise ValueError("Holdout labels and typed requests differ")
    if any(
        normalize_text(row.request.description) != normalize_text(
            by_id[row.query_id].description
        )
        for row in cases
    ):
        raise ValueError("Holdout normalization or provenance changed")

    ranked = benchmark_holdout()
    by_reason: dict[str, dict[str, Any]] = {
        label: {
            "cases": 0, "draft_auto_selected": 0,
            "draft_needs_review": 0, "draft_no_match": 0,
            "ml_false_suggestions": 0, "tfidf_false_suggestions": 0,
            "false_suggestions_without_draft_auto_selection": 0,
        }
        for label in sorted(_ALLOWED_REASONS)
    }
    ids_by_mode: dict[str, set[str]] = {}
    for mode in ("ml", "tfidf"):
        output = ranked["comparisons"][mode]
        rows = output["cases"]
        if len(rows) != len(cases):
            raise AssertionError("Suggestion benchmark omitted held-out cases")
        seen = {row["id"] for row in rows}
        if len(seen) != len(cases) or seen != set(by_id):
            raise AssertionError("Suggestion case set differs from draft holdout")
        ids_by_mode[mode] = {
            row["id"] for row in rows if row["suggested_sku"] is not None
        }

    products = list(training.products.values())
    draft = audit_auto_selection(cases, products)
    decisions: list[dict[str, str | bool | None]] = []
    for record in cases:
        label = by_id[record.query_id].reason
        if label not in _ALLOWED_REASONS or record.relevant_skus:
            raise ValueError("Expected only verified-shape no-match cases")
        single = build_auto_draft([record.request], products)
        decision = single.lines[0]
        if len(single.lines) != 1 or decision.status not in (
            "auto_selected", "needs_review", "no_match"
        ):
            raise AssertionError("Actual quote draft status unsupported")
        cell = by_reason[label]
        cell["cases"] += 1
        cell["draft_" + decision.status] += 1
        for mode, accepted_ids in ids_by_mode.items():
            if record.query_id in accepted_ids:
                cell[mode + "_false_suggestions"] += 1
        if (
            record.query_id in ids_by_mode["ml"]
            or record.query_id in ids_by_mode["tfidf"]
        ) and decision.status != "auto_selected":
            cell["false_suggestions_without_draft_auto_selection"] += 1
        decisions.append({
            "id": record.query_id,
            "synthetic_reason": label,
            "ml_ranker_suggests": record.query_id in ids_by_mode["ml"],
            "tfidf_ranker_suggests": record.query_id in ids_by_mode["tfidf"],
            "auto_draft_status": decision.status,
            "draft_reason_codes": ",".join(decision.reason_codes) or None,
        })
    if (
        Counter(by_id[row.query_id].reason for row in cases) !=
        Counter({name: cell["cases"] for name, cell in by_reason.items()})
    ):
        raise AssertionError("Held-out negative family accounting differs")
    if (
        sum(cell["draft_auto_selected"] for cell in by_reason.values())
        != draft.auto_selected
        or sum(cell["draft_no_match"] for cell in by_reason.values())
        + sum(cell["draft_needs_review"] for cell in by_reason.values())
        != draft.abstained
        or draft.no_match_queries != len(cases)
    ):
        raise AssertionError("Quote draft decisions inconsistent across auditors")
    for mode in ids_by_mode:
        if (
            sum(cell[mode + "_false_suggestions"] for cell in by_reason.values())
            != ranked["comparisons"][mode]["false_suggestions"]
        ):
            raise AssertionError("Ranker false suggestions not reconciled by family")
    return {
        "kind": "kpdesk_synthetic_semantic_no_match_draft_audit_v1",
        "evaluation_scope": "existing frozen 22 hand-authored synthetic no-match cases",
        "provenance": {
            "kind": "synthetic_example",
            "holdout_sha256": hashlib.sha256(_HOLDOUT.read_bytes()).hexdigest(),
            "independent_customer_labels": False,
            "training_data_source_sha256": training.source_sha256,
            "thresholds_recalibrated": False,
            "llm_calls": 0,
            "production_eligible": False,
        },
        "reasons": by_reason,
        "ranker_thresholds": {
            mode: ranked["comparisons"][mode]["frozen_validation_threshold"]
            for mode in ("ml", "tfidf")
        },
        "real_auto_draft": asdict(draft),
        "ranker_false_suggestions": {
            mode: ranked["comparisons"][mode]["false_suggestions"]
            for mode in ("ml", "tfidf")
        },
        "automated_selection_safety_gate": {
            "pass_on_this_synthetic_holdout": draft.false_auto_selected_no_match == 0,
            "production_authorization": False,
            "manual_review_remains_required": True,
        },
        "cases": decisions,
        "limitations": (
            "The 8 semantic-absence labels are hand-authored and lack independent "
            "real supplier/customer adjudication. No supplier licensing inferred. "
            "This tests actual auto-draft decisions, not the external PDF or API. "
            "No threshold tuning or product model changes were performed."
        ),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    destination = args.output.resolve()
    artifacts = (PROJECT_ROOT / "artifacts").resolve()
    if (
        destination.parent != artifacts or destination.suffix != ".json"
        or not artifacts.is_dir() or artifacts.is_symlink()
        or args.output.exists()
    ):
        raise ValueError("Audit output must be a new JSON file in project artifacts")
    report = audit_no_match_draft_safety()
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2, sort_keys=True)
        output.write("\n")
    print(json.dumps({
        "reasons": report["reasons"],
        "ranker_false_suggestions": report["ranker_false_suggestions"],
        "actual_draft": report["real_auto_draft"],
        "production_authorization": False,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
