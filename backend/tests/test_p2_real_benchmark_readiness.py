"""P2 real request source provenance audit: no gold laundering, no text leak."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from kpdesk.matching.real_requests import load_request_intake
from scripts.audit_p2_real_benchmark_readiness import (
    INTAKE_PATH,
    SOURCE_MANIFEST,
    SOURCE_ROOT,
    _manifest,
    audit_readiness,
    build_report,
    main,
)


def test_actual_corpus_is_not_mistaken_for_independent_benchmark() -> None:
    report = build_report()
    assert report["request_fragments"] == 53
    assert report["source_documents"] == 5
    assert report["declared_source_batches"] == 5
    assert report["independent_buyer_groups"] == 4
    assert report["source_documents_per_buyer_group_sorted"] == [1, 1, 1, 2]
    assert report["request_fragments_per_buyer_group_sorted"] == [1, 12, 16, 24]
    assert report["source_checked_adjacent_quantity_evidence"] == 27
    assert report["document_hosts"] == 1
    assert report["three_way_source_group_split_allowed"] is False
    assert report["independent_source_disjoint_sku_gold"] is False
    assert report["training_approved"] is False
    assert report["benchmark_approved"] is False
    assert report["real_world_accuracy_metrics"] is None
    blockers = report["blocked_reasons"]
    assert isinstance(blockers, list)
    assert "insufficient_independent_buyer_groups_for_3way_split" in blockers


def test_emitted_metadata_has_no_request_text_or_source_names() -> None:
    report = build_report()
    output = json.dumps(report, ensure_ascii=False, sort_keys=True)
    source = load_request_intake(INTAKE_PATH, SOURCE_ROOT)
    assert all(
        record.request_text not in output and record.source_quote not in output
        and record.source_file not in output and record.source_batch_id not in output
        and (record.buyer_group_id is None or record.buyer_group_id not in output)
        for record in source.records
    )


@pytest.mark.parametrize("mutation", [
    "altered_sha", "elevated_rights", "fake_gold", "missing_source", "duplicate_source",
])
def test_manifest_status_cannot_mint_gold(mutation: str) -> None:
    intake = load_request_intake(INTAKE_PATH, SOURCE_ROOT)
    manifest = copy.deepcopy(_manifest(SOURCE_MANIFEST))
    entries = manifest["entries"]
    if mutation == "altered_sha":
        entries[0]["sha256"] = "0" * 64
    elif mutation == "elevated_rights":
        entries[0]["training_rights_reviewed"] = True
    elif mutation == "fake_gold":
        entries[0]["human_matching_labels"] = 200
    elif mutation == "missing_source":
        entries.pop()
    elif mutation == "duplicate_source":
        entries.append(copy.deepcopy(entries[0]))
    with pytest.raises(ValueError, match="Source|Duplicate|different"):
        audit_readiness(intake, manifest)


def test_readiness_cli_is_create_only_and_leaks_no_source_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "readiness.json"
    assert main(["--output", str(path)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == {
        "request_fragments": 53,
        "source_documents": 5,
        "independent_buyer_groups": 4,
        "three_way_source_group_split_allowed": False,
        "benchmark_approved": False,
    }
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["independent_source_disjoint_sku_gold"] is False
    assert "source_text" not in json.dumps(saved)
    before = path.read_bytes()
    with pytest.raises(SystemExit):
        main(["--output", str(path)])
    assert before == path.read_bytes()
