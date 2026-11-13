from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scripts.audit_recent_supplier_candidates import (
    EXPECTED,
    ROOT,
    audit_recent_supplier_candidates,
)


def _copied_corpus(tmp_path: Path) -> Path:
    target = tmp_path / "research"
    target.mkdir()
    shutil.copyfile(ROOT / "manifest.json", target / "manifest.json")
    for source in EXPECTED.values():
        shutil.copyfile(ROOT / source[2], target / source[2])
    return target


def test_new_sources_are_verified_as_quarantined_only() -> None:
    report = audit_recent_supplier_candidates()
    assert report["source_count"] == 6
    assert report["structural_positive_price_candidates_not_currency_verified"] == 10_066
    assert report["native_rub_price_rows_verified"] == 21_769
    assert report["formula_cached_price_rows_unverified"] == 1_035
    assert report["training_authorized"] is False
    findings = report["sources"]
    assert isinstance(findings, list)
    assert all(item["original_integrity_verified"] for item in findings)
    assert all(item["canonical_importable"] is False for item in findings)


def test_modified_original_is_rejected(tmp_path: Path) -> None:
    root = _copied_corpus(tmp_path)
    path = root / "viz_tools_20261019.xlsx"
    with path.open("r+b") as file:
        file.seek(1234)
        byte = file.read(1)
        file.seek(1234)
        file.write(bytes([byte[0] ^ 1]))
    with pytest.raises(ValueError, match="checksum"):
        audit_recent_supplier_candidates(root)


@pytest.mark.parametrize(
    ("source_id", "field", "value", "error"),
    [
        ("viz_tools_20261019", "source_file_url", "https://example.com/x.xlsx", "changed"),
        ("viz_tools_20261019", "native_rub_verified", True, "changed"),
        ("avt_el_ecoplast_20260615", "training_authorized", True, "promoted"),
        ("etk_rezerv_20260316", "training_authorized", True, "promoted"),
        ("shveigid_20261021", "training_authorized", True, "promoted"),
        ("technoas_20260916", "native_rub_verified", True, "changed"),
        ("etk_rezerv_20260316", "formula_cached_positive_rows_unverified", 0, "mismatch"),
        ("dselektric_20261021", "training_authorized", True, "promoted"),
        ("viz_tools_20261019", "structural_positive_price_candidates", 999, "changed"),
    ],
)
def test_manifest_cannot_promote_or_change_source(
    tmp_path: Path, source_id: str, field: str, value: object, error: str,
) -> None:
    root = _copied_corpus(tmp_path)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    record = next(item for item in manifest["sources"] if item["source_id"] == source_id)
    record[field] = value
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match=error):
        audit_recent_supplier_candidates(root)

def test_ecoplast_original_uses_explicit_rub_and_excludes_bad_rows() -> None:
    from scripts.audit_recent_supplier_candidates import _audit_ecoplast

    report = _audit_ecoplast(ROOT / "avt_el_ecoplast_20260615.xlsx")
    assert report == {
        "identity_rows": 3_612,
        "native_rub_labeled_rows": 3_603,
        "candidate_native_rub_price_rows": 3_595,
        "missing_or_invalid_price_rows": 6,
        "invalid_currency_rows": 9,
        "invalid_package_rows": 2,
        "duplicate_sku_groups": 4,
    }


def test_ecoplast_changed_currency_reduces_verified_candidates(tmp_path: Path) -> None:
    from openpyxl import load_workbook

    from scripts.audit_recent_supplier_candidates import _audit_ecoplast

    original = ROOT / "avt_el_ecoplast_20260615.xlsx"
    changed = tmp_path / "ecoplast_changed.xlsx"
    shutil.copyfile(original, changed)
    book = load_workbook(changed)
    book.worksheets[0]["F6"] = "USD"
    book.save(changed)
    book.close()

    report = _audit_ecoplast(changed)
    assert report["candidate_native_rub_price_rows"] == 3_594
    assert report["invalid_currency_rows"] == 10

def test_etk_multisheet_rub_prices_keep_original_sheet_dates() -> None:
    from scripts.audit_recent_supplier_candidates import _audit_etk_rezerv

    result = _audit_etk_rezerv(ROOT / "etk_rezerv_20260316.xlsx")
    assert result["candidate_native_rub_price_rows"] == 252
    assert result["formula_cached_positive_rows_unverified"] == 1_035
    assert result["source_sku_provided"] is False
    assert result["sheet_literal_counts"] == [59, 9, 9, 7, 129, 17, 4, 18]
    assert result["sheet_formula_counts"] == [820, 9, 9, 7, 31, 127, 14, 18]
    assert result["sheet_years"] == [2026, 2026, 2025, 2026, 2024, 2024, 2026, 2026]


def test_etk_changed_rub_heading_is_rejected(tmp_path: Path) -> None:
    from openpyxl import load_workbook

    from scripts.audit_recent_supplier_candidates import _audit_etk_rezerv

    changed = tmp_path / "etk_modified.xlsx"
    shutil.copyfile(ROOT / "etk_rezerv_20260316.xlsx", changed)
    workbook = load_workbook(changed)
    workbook.worksheets[0]["H9"] = "Price USD"
    workbook.save(changed)
    workbook.close()
    with pytest.raises(ValueError, match="currency heading"):
        _audit_etk_rezerv(changed)
