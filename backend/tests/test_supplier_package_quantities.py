"""Typed sale-package sizes preserve integral Excel numeric values, never infer units."""
from __future__ import annotations

import io
from decimal import Decimal

import pytest
from openpyxl import Workbook

from kpdesk.catalog.schema import ImportLimits
from kpdesk.catalog.supplier_staging import (
    SupplierMapping,
    _stage_source_rows,
    stage_supplier_prices,
)

MAPPING = SupplierMapping(
    sku=0, name=1, price=2, currency=3, sale_unit=4,
    pieces_per_sale_unit=5, header_row=1,
)


@pytest.mark.parametrize(("source_cell", "expected", "invalid"), [
    (12, 12, False),
    (12.0, 12, False),
    ("12", 12, False),
    ("00012", 12, False),
    (100_000.0, 100_000, False),
    (100_001.0, None, True),
    ("100001", None, True),
    (5.7, None, True),
    ("5.7", None, True),
    ("12.0", None, True),
    (0, None, True),
    (-2, None, True),
    (True, None, True),
    (float("nan"), None, True),
    (float("inf"), None, True),
    ("1e2", None, True),
])
def test_excel_numeric_package_size_and_ambiguous_literals(
    source_cell: object, expected: int | None, invalid: bool,
) -> None:
    source: list[tuple[int, list[object]]] = [
        (1, ["sku", "name", "price", "currency", "unit", "pack"]),
        (2, ["SKU-0001", "Box of clips", "120", "RUB", "box", source_cell]),
    ]
    result = _stage_source_rows("supplier.xlsx", b"source", MAPPING, source, ImportLimits())
    row = result.rows[0]
    assert row.source_pack_size == expected
    assert ("invalid_pack_size" in row.issues) is invalid
    assert result.canonical_importable is False
    assert result.model_training_authorized is False


def test_real_xlsx_numeric_package_is_staged_without_unit_guess() -> None:
    book = Workbook()
    page = book.active
    assert page is not None
    page.title = "Supplier"
    page.append(["sku", "name", "price", "currency", "unit", "pack"])
    page.append(["K-12", "Clips pack", 250.50, "RUB", "уп", 12])
    page.append(["K-13", "Tape by metre", 99.99, "RUB", "м", 5.7])
    file = io.BytesIO()
    book.save(file)
    book.close()

    stage = stage_supplier_prices(
        "supplier.xlsx", file.getvalue(),
        SupplierMapping(sku=0, name=1, price=2, currency=3,
                        sale_unit=4, pieces_per_sale_unit=5,
                        header_row=1, sheet="Supplier"),
    )
    assert [row.source_pack_size for row in stage.rows] == [12, None]
    assert "invalid_pack_size" not in stage.rows[0].issues
    assert "invalid_pack_size" in stage.rows[1].issues
    assert [row.source_sale_unit for row in stage.rows] == ["уп", "м"]
    assert stage.canonical_importable is False
    assert stage.model_training_authorized is False


def test_price_cannot_be_derived_from_pack_quantity_without_review() -> None:
    source: list[tuple[int, list[object]]] = [
        (1, ["sku", "name", "price", "currency", "unit", "pack"]),
        (2, ["SKU-0001", "Bundle", Decimal("500.00"), "RUB", "уп", 10.0]),
    ]
    result = _stage_source_rows("supplier.xlsx", b"source", MAPPING, source, ImportLimits())
    assert result.rows[0].source_pack_size == 10
    assert str(result.rows[0].price_rub) == "500.00"
    assert result.rows[0].source_sale_unit == "уп"
    assert result.canonical_importable is False


def test_unmapped_excel_formula_does_not_bypass_source_safety_gate() -> None:
    book = Workbook()
    page = book.active
    assert page is not None
    page.title = "Supplier"
    page.append(["sku", "name", "price", "currency", "unit", "pack", "extra"])
    page.append(["K-12", "Bundle", 500, "RUB", "уп", 12, "=2+2"])
    file = io.BytesIO()
    book.save(file)
    book.close()
    with pytest.raises(ValueError, match="formula"):
        stage_supplier_prices(
            "supplier.xlsx", file.getvalue(),
            SupplierMapping(
                sku=0, name=1, price=2, currency=3,
                sale_unit=4, pieces_per_sale_unit=5,
                header_row=1, sheet="Supplier",
            ),
        )


@pytest.mark.parametrize(("pack_value", "expected_issue"), [
    (None, "missing_pack_size"),
    ("", "missing_pack_size"),
    ("   ", "missing_pack_size"),
    (0, "invalid_pack_size"),
    ("12.0", "invalid_pack_size"),
    (False, "invalid_pack_size"),
])
def test_mapped_supplier_pack_distinguishes_absent_from_invalid(
    pack_value: object, expected_issue: str,
) -> None:
    rows: list[tuple[int, list[object]]] = [
        (1, ["sku", "name", "price", "currency", "unit", "pack"]),
        (2, ["SKU-01", "Vendor goods", 240, "RUB", "шт", pack_value]),
    ]
    stage = _stage_source_rows("vendor.xlsx", b"source", MAPPING, rows, ImportLimits())
    row = stage.rows[0]
    assert row.source_pack_size is None
    assert expected_issue in row.issues
    assert ("invalid_pack_size" in row.issues) is (expected_issue == "invalid_pack_size")
    assert ("missing_pack_size" in row.issues) is (expected_issue == "missing_pack_size")
    assert stage.model_training_authorized is False


def test_pack_column_not_mapped_is_distinct_from_blank_mapped_cell() -> None:
    rows: list[tuple[int, list[object]]] = [
        (1, ["sku", "name", "price", "currency", "unit", "pack"]),
        (2, ["SKU-01", "Vendor goods", 240, "RUB", "шт", ""]),
    ]
    unmapped = SupplierMapping(
        sku=0, name=1, price=2, currency=3, sale_unit=4, header_row=1,
    )
    result = _stage_source_rows("vendor.csv", b"source", unmapped, rows, ImportLimits())
    assert "pack_size_requires_review" in result.rows[0].issues
    assert "missing_pack_size" not in result.rows[0].issues
