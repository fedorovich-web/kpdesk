"""Explicit XLSX formula quarantine retains row evidence without accepting formulas."""
from __future__ import annotations

import io
import uuid
from dataclasses import replace

import pytest
from openpyxl import Workbook

from kpdesk.catalog.supplier_canonical_preview import (
    CanonicalExportRejected,
    ReviewedExclusion,
    ReviewedProductDecision,
    preview_canonical_catalog,
    source_row_sha256,
)
from kpdesk.catalog.supplier_staging import SupplierMapping, stage_supplier_prices
from kpdesk.db.tenant_access import AuthenticatedUser


def _source() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Prices"
    sheet.append(["SKU", "Name", "Price", "Currency", "Unit", "Pack", "Auxiliary"])
    sheet.append(["A-01", "Good keyboard", 120, "RUB", "шт", 1, "ordinary"])
    sheet.append(["B-02", "Price as formula", "=50+70", "RUB", "шт", 1, None])
    sheet.append(["C-03", "Currency as formula", 100, '="RUB"', "шт", 1, None])
    sheet.append(["D-04", "Formula in auxiliary", 800, "RUB", "шт", 1, "=1+1"])
    sheet.append(["=1+1", "Formula SKU", 90, "RUB", "шт", 1, None])
    sheet.append(["E-05", "Formula pack", 99, "RUB", "уп", "=10+2", None])
    sheet.append(["F-06", "=1+1", 99, "RUB", "шт", 1, None])
    stream = io.BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


MAPPING = SupplierMapping(
    sku=0, name=1, price=2, currency=3, sale_unit=4,
    pieces_per_sale_unit=5, header_row=1, sheet="Prices",
)


def test_formula_quarantine_is_explicit_opt_in_and_safe_per_row() -> None:
    content = _source()
    with pytest.raises(ValueError, match="formula"):
        stage_supplier_prices("supplier.xlsx", content, MAPPING)
    stage = stage_supplier_prices(
        "supplier.xlsx", content, MAPPING, formula_policy="quarantine_rows",
    )
    assert len(stage.rows) == 7
    assert stage.rows[0].supplier_sku == "A-01"
    assert str(stage.rows[0].price_rub) == "120"
    assert stage.rows[0].formula_columns == ()
    assert stage.rows[0].issues == ("category_requires_review",)
    assert stage.rows[1].line == 3
    assert stage.rows[1].formula_columns == (2,)
    assert stage.rows[1].price_rub is None
    assert "invalid_or_missing_price" in stage.rows[1].issues
    assert stage.rows[2].formula_columns == (3,)
    assert stage.rows[2].price_rub is None
    assert "unconfirmed_or_non_rub_currency" in stage.rows[2].issues
    assert stage.rows[3].formula_columns == (6,)
    assert str(stage.rows[3].price_rub) == "800"
    assert stage.rows[4].supplier_sku is None
    assert stage.rows[4].formula_columns == (0,)
    assert "missing_supplier_sku" in stage.rows[4].issues
    assert stage.rows[5].source_pack_size is None
    assert stage.rows[5].formula_columns == (5,)
    assert "invalid_pack_size" in stage.rows[5].issues
    assert stage.rows[6].name == ""
    assert stage.rows[6].formula_columns == (1,)
    assert "missing_name" in stage.rows[6].issues
    assert all(
        "xlsx_formula_cell_quarantined" in row.issues for row in stage.rows[1:]
    )
    assert stage.canonical_importable is False
    assert stage.model_training_authorized is False


def test_formula_metadata_binds_review_row_and_blocks_canonical_export() -> None:
    source = stage_supplier_prices(
        "supplier.xlsx", _source(), MAPPING, formula_policy="quarantine_rows",
    )
    row = source.rows[3]
    assert source_row_sha256(row) != source_row_sha256(
        replace(row, formula_columns=()),
    )
    decision = ReviewedProductDecision(
        line=row.line, source_row_sha256=source_row_sha256(row),
        category="keyboard", sale_unit="штука", pieces_per_sale_unit=1,
        confirmed=True, attributes={"connection": "wired", "keys": 104},
    )
    with pytest.raises(CanonicalExportRejected, match="unverified_xlsx_formula_provenance"):
        preview_canonical_catalog(
            source,
            tuple(
                decision if current.line == row.line else ReviewedExclusion(
                    line=current.line,
                    source_row_sha256=source_row_sha256(current),
                    confirmed=True,
                    reason="Source row requires independent manual validation",
                )
                for current in source.rows
            ),
            organization_id=uuid.UUID("60000000-0000-4000-8000-000000000001"),
            reviewer=AuthenticatedUser(uuid.UUID("60000000-0000-4000-8000-000000000002")),
            expected_source_sha256=source.sha256,
            price_basis="standard",
            explicitly_confirmed_price_basis=True,
        )


def test_unsupported_policy_is_rejected_before_parsing() -> None:
    with pytest.raises(ValueError, match="formula_policy"):
        stage_supplier_prices(
            "supplier.xlsx", _source(), MAPPING,
            formula_policy="evaluate",  # type: ignore[arg-type]
        )


def test_formula_metadata_keeps_prior_hash_for_formula_free_rows() -> None:
    import hashlib
    import json

    stage = stage_supplier_prices(
        "supplier.xlsx", _source(), MAPPING, formula_policy="quarantine_rows",
    )
    row = stage.rows[0]
    assert row.formula_columns == ()
    legacy_representation = {
        "line": row.line,
        "supplier_sku": row.supplier_sku,
        "name": row.name,
        "price_rub": str(row.price_rub) if row.price_rub is not None else None,
        "source_category": row.source_category,
        "source_sale_unit": row.source_sale_unit,
        "source_pack_size": row.source_pack_size,
        "issues": row.issues,
    }
    expected = hashlib.sha256(
        json.dumps(
            legacy_representation, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert source_row_sha256(row) == expected


def test_ecoplast_original_quarantine_is_precise_and_never_promotes_prices() -> None:
    from collections import Counter
    from pathlib import Path

    from openpyxl import load_workbook

    original = (
        Path(__file__).resolve().parents[2]
        / "ml/data/real/research_20261021_supplier_prices/avt_el_ecoplast_20260615.xlsx"
    )
    if not original.is_file():
        pytest.skip("Pinned research-only original not mounted in this workspace.")
    raw = original.read_bytes()
    book = load_workbook(original, read_only=True, data_only=False, keep_links=False)
    try:
        sheet_name = book.worksheets[0].title
    finally:
        book.close()
    source_mapping = SupplierMapping(
        sku=0, name=1, price=4, currency=5,
        sale_unit=2, pieces_per_sale_unit=3,
        header_row=2, sheet=sheet_name,
    )
    with pytest.raises(ValueError, match="formula"):
        stage_supplier_prices(original.name, raw, source_mapping)
    staged = stage_supplier_prices(
        original.name, raw, source_mapping, formula_policy="quarantine_rows",
    )
    counts: Counter[int] = Counter()
    for record in staged.rows:
        for column in record.formula_columns:
            counts[column] += 1
    assert len(staged.rows) == 4179
    assert counts == {5: 6, 10: 6}
    # Original source: 567 blank mapped pack cells are NOT the same as
    # two non-integer/invalid pack values. Neither becomes a gold label.
    assert sum("missing_pack_size" in row.issues for row in staged.rows) == 567
    assert sum("invalid_pack_size" in row.issues for row in staged.rows) == 2
    assert len([row for row in staged.rows if row.formula_columns]) == 12
    assert all(
        "xlsx_formula_cell_quarantined" in row.issues
        for row in staged.rows if row.formula_columns
    )
    assert staged.canonical_importable is False
    assert staged.model_training_authorized is False
