"""Raw adjacent source-cell quantities cannot be invented or silently normalized."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from kpdesk.matching.real_requests import load_request_intake
from kpdesk.matching.source_evidence import extract_xlsx_row_amount
from scripts.prepare_public_procurement_intake import build_unreviewed_procurement_intake

SOURCE = Path(__file__).resolve().parents[2] / "ml/data/real/public_procurement_requests"


def test_real_xlsx_qty_unit_are_direct_evidence_only() -> None:
    workbook = SOURCE / "bidzaar_339_280_stationery_2026.xlsx"
    records = build_unreviewed_procurement_intake(SOURCE)["records"]
    rows = [row for row in records if row["source_file"].endswith(".xlsx")]
    assert len(rows) == 11
    assert all(row["quantity_evidence"]["verified_as"] == "raw_adjacent_cells" for row in rows)
    assert {row["quantity_evidence"]["quantity"] for row in rows} >= {600, 300, 15}
    assert all(row["quantity_evidence"]["unit_cell"].replace("D", "E") ==
               row["quantity_evidence"]["quantity_cell"] for row in rows)
    assert rows[0]["quantity_evidence"]["unit_original"] == "уп."
    assert extract_xlsx_row_amount(
        workbook, rows[0]["source_quote"],
        expected_sha256=rows[0]["source_sha256"],
    ) == rows[0]["quantity_evidence"]


def test_intake_validates_adjacent_cells_and_rejects_fabricated_amount(
    tmp_path: Path,
) -> None:
    data = build_unreviewed_procurement_intake(SOURCE)
    path = tmp_path / "intake.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert load_request_intake(path, SOURCE).source_excerpts_verified
    rows = [row for row in data["records"] if row["source_file"].endswith(".xlsx")]
    original = rows[0]["quantity_evidence"]["quantity"]
    rows[0]["quantity_evidence"]["quantity"] = original + 1
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="quantity|source.*evidence|adjacent"):
        load_request_intake(path, SOURCE)


def test_intake_cannot_attach_xlsx_cells_to_docx_source(tmp_path: Path) -> None:
    data = build_unreviewed_procurement_intake(SOURCE)
    xlsx = next(row for row in data["records"] if row["source_file"].endswith(".xlsx"))
    docx = next(row for row in data["records"] if row["source_file"].endswith(".docx"))
    docx["quantity_evidence"] = xlsx["quantity_evidence"]
    path = tmp_path / "intake.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="quantity|XLSX|xlsx"):
        load_request_intake(path, SOURCE)
