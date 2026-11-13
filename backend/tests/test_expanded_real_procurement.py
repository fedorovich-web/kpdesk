"""The expanded public-tender corpus must preserve buyer grouping and unknowns."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from kpdesk.matching.real_requests import build_request_split_manifest, load_request_intake
from scripts.prepare_public_procurement_intake import build_unreviewed_procurement_intake

SOURCE = Path(__file__).resolve().parents[2] / "ml/data/real/public_procurement_requests"


def test_five_source_documents_four_buyer_groups_and_unknown_gold(tmp_path: Path) -> None:
    original = build_unreviewed_procurement_intake(SOURCE)
    assert len(original["records"]) == 24
    expanded = build_unreviewed_procurement_intake(SOURCE, manifest_name="manifest-v2.json")
    rows = expanded["records"]
    assert 45 <= len(rows) <= 70
    assert len({item["source_file"] for item in rows}) == 5
    assert len({item["source_batch_id"] for item in rows}) == 5
    assert len({item["buyer_group_id"] for item in rows}) == 4
    assert all(item["rights_status"] == "review_required" for item in rows)
    assert all(item["source_quote"] == item["request_text"] for item in rows)
    assert all("relevant_skus" not in item for item in rows)
    saved = tmp_path / "intake.json"
    saved.write_text(json.dumps(expanded, ensure_ascii=False), encoding="utf-8")
    checked = load_request_intake(saved, SOURCE)
    assert checked.source_excerpts_verified is True
    assert checked.training_approved is False
    with pytest.raises(ValueError, match="groups"):
        build_request_split_manifest(checked)


def test_quantity_evidence_obeys_explicit_source_column_layout() -> None:
    records = build_unreviewed_procurement_intake(SOURCE, manifest_name="manifest-v2.json")["records"]
    design = [x for x in records if x["source_file"].endswith("design_2026.xlsx")]
    tmc = [x for x in records if x["source_file"].endswith("tmc_2026.xlsx")]
    assert len(design) == 16
    assert 8 <= len(tmc) <= 12
    assert all(item["quantity_evidence"]["unit_original"] is None for item in design)
    assert all(item["quantity_evidence"]["quantity_cell"].endswith(
        "D" + str(int(item["quantity_evidence"]["quantity_cell"].rsplit("D", 1)[-1]))
    ) for item in design)
    assert all(item["quantity_evidence"] is None for item in tmc)


def test_same_organizer_buyer_group_cannot_leak_between_batches(tmp_path: Path) -> None:
    rows = build_unreviewed_procurement_intake(SOURCE, manifest_name="manifest-v2.json")["records"]
    akron = [r for r in rows if r["buyer_group_id"] == "akron_holding"]
    assert len({r["source_batch_id"] for r in akron}) == 2
    assert len({r["buyer_group_id"] for r in akron}) == 1
    path = tmp_path / "intake.json"
    path.write_text(json.dumps({"schema_version":"real-request-intake-v1","records":rows},
                               ensure_ascii=False), encoding="utf-8")
    checked=load_request_intake(path,SOURCE)
    with pytest.raises(ValueError,match="groups"):
        build_request_split_manifest(checked)


def test_expanded_intake_does_not_edit_old_source_manifest(tmp_path: Path) -> None:
    old = (SOURCE / "manifest.json").read_bytes()
    expanded = build_unreviewed_procurement_intake(SOURCE, manifest_name="manifest-v2.json")
    assert len(expanded["records"]) > 24
    assert (SOURCE / "manifest.json").read_bytes() == old

def test_saved_v3_is_reproducible_and_has_no_approved_labels() -> None:
    """The on-disk 5-document snapshot must not drift away from originals."""
    target = SOURCE / "intake-unreviewed-v3.json"
    saved = json.loads(target.read_text(encoding="utf-8"))
    expected = build_unreviewed_procurement_intake(
        SOURCE, manifest_name="manifest-v2.json",
    )
    assert saved == expected
    assert len(saved["records"]) == 53
    assert len({row["source_batch_id"] for row in saved["records"]}) == 5
    assert len({row["buyer_group_id"] for row in saved["records"]}) == 4
    assert all("relevant_skus" not in row for row in saved["records"])
    verified = load_request_intake(target, SOURCE)
    assert verified.source_excerpts_verified is True
    assert verified.training_approved is False
