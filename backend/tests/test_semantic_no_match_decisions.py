"""Regress ranker/draft distinction for frozen synthetic no-match cases."""
from __future__ import annotations

from pathlib import Path

import pytest

from kpdesk.matching.cli import load_dataset
from kpdesk.matching.retrieve import retrieve_candidates
from scripts.audit_semantic_no_match_decisions import (
    audit_no_match_draft_safety,
    main,
)
from scripts.evaluate_no_match_holdout import (
    _HOLDOUT,
    _SOURCE,
    HoldoutDocument,
    load_holdout,
)


def test_frozen_semantic_negative_cases_are_real_retrieval_challenges() -> None:
    training = load_dataset(_SOURCE)
    cases = load_holdout(_HOLDOUT, training.queries, training.products)
    annotations = HoldoutDocument.model_validate_json(_HOLDOUT.read_bytes())
    semantic_ids = {case.id for case in annotations.cases
                    if case.reason == "semantic_absence"}
    assert len(semantic_ids) == 8
    catalog = list(training.products.values())
    by_id = {case.query_id: case for case in cases}
    assert all(
        len(retrieve_candidates(by_id[query_id].request, catalog)) > 0
        for query_id in semantic_ids
    )


def test_suggestions_are_not_counted_as_auto_selected_quote_products() -> None:
    report = audit_no_match_draft_safety()
    assert report["provenance"]["kind"] == "synthetic_example"
    assert report["provenance"]["independent_customer_labels"] is False
    assert report["provenance"]["thresholds_recalibrated"] is False
    assert report["provenance"]["production_eligible"] is False
    assert report["automated_selection_safety_gate"] == {
        "pass_on_this_synthetic_holdout": True,
        "production_authorization": False,
        "manual_review_remains_required": True,
    }
    assert len(report["cases"]) == 22
    assert len({case["id"] for case in report["cases"]}) == 22
    assert report["reasons"]["semantic_absence"]["cases"] == 8
    assert report["reasons"]["semantic_absence"]["ml_false_suggestions"] == 7
    assert report["reasons"]["semantic_absence"]["tfidf_false_suggestions"] == 7
    assert report["real_auto_draft"]["auto_selected"] == 0
    assert report["real_auto_draft"]["false_auto_selected_no_match"] == 0
    assert report["real_auto_draft"]["abstained"] == 22
    assert all(
        case["auto_draft_status"] in ("needs_review", "no_match")
        for case in report["cases"]
    )
    assert sum(
        cell["cases"] for cell in report["reasons"].values()
    ) == 22


def test_audit_cli_rejects_output_outside_artifacts(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="project artifacts"):
        main(["--output", str(tmp_path / "outside.json")])
