"""Bounded quarantine acceptance tests for mixed-currency and VAT-only XLSX."""
from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import load_workbook

from scripts.audit_supplier_additions import audit_shveigid, audit_technoas

ROOT = Path(__file__).resolve().parents[2] / "ml/data/real/research_20261021_supplier_prices"


def test_shveigid_counts_native_rub_without_double_counting_mirrored_sheets() -> None:
    report = audit_shveigid(ROOT / "shveigid_20261021.xlsx")
    assert report["source_price_rows"] == 21_858
    assert report["original_rub_currency_rows"] == 20_168
    assert report["foreign_currency_rows_excluded"] == 1_690
    assert report["native_rub_price_candidates"] == 17_922
    assert report["missing_article_in_accepted_rows"] == 3_682
    assert report["duplicate_internal_id_groups"] == 0


def test_technoas_vat_column_does_not_imply_currency() -> None:
    report = audit_technoas(ROOT / "technoas_20260916.xlsx")
    assert report["source_document_date"] == "2026-03-19"
    assert report["literal_positive_vat_price_rows"] == 354
    assert report["calculated_non_vat_formula_rows"] == 352
    assert report["native_rub_price_candidates"] == 0


def test_technoas_changed_price_heading_is_rejected(tmp_path: Path) -> None:
    from shutil import copyfile

    target = tmp_path / "technoas_bad_heading.xlsx"
    copyfile(ROOT / "technoas_20260916.xlsx", target)
    workbook = load_workbook(target)
    workbook.worksheets[0]["G10"] = "USD"
    workbook.save(target)
    workbook.close()

    with pytest.raises(ValueError, match="VAT price heading"):
        audit_technoas(target)
