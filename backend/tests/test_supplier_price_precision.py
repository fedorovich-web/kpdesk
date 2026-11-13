"""High precision supplier prices are source evidence until explicitly reviewed."""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from kpdesk.catalog.schema import ImportLimits
from kpdesk.catalog.supplier_canonical_preview import (
    CanonicalExportRejected,
    CanonicalPreview,
    ReviewedPrecisionPrice,
    ReviewedProductDecision,
    preview_canonical_catalog,
    source_row_sha256,
)
from kpdesk.catalog.supplier_staging import (
    SupplierMapping,
    _stage_source_rows,
    stage_supplier_prices,
)
from kpdesk.db.tenant_access import AuthenticatedUser

ORG = uuid.UUID("70000000-0000-4000-8000-000000000001")
REVIEWER = AuthenticatedUser(uuid.UUID("70000000-0000-4000-8000-000000000002"))
MAPPING = SupplierMapping(
    sku=0, name=1, price=2, currency=3, sale_unit=4, pieces_per_sale_unit=5,
    header_row=1,
)


def _preview(content: bytes, decision: ReviewedProductDecision) -> CanonicalPreview:
    stage = stage_supplier_prices("vendor.csv", content, MAPPING)
    return preview_canonical_catalog(
        stage, (decision,), organization_id=ORG, reviewer=REVIEWER,
        expected_source_sha256=stage.sha256, price_basis="standard",
        explicitly_confirmed_price_basis=True,
    )


def _decision(content: bytes) -> ReviewedProductDecision:
    stage = stage_supplier_prices("vendor.csv", content, MAPPING)
    return ReviewedProductDecision(
        line=stage.rows[0].line, source_row_sha256=source_row_sha256(stage.rows[0]),
        category="stationery_other", sale_unit="штука",
        pieces_per_sale_unit=1, confirmed=True,
        attributes={"product_kind": "item"},
    )


def test_structural_overprecision_distinct_from_corrupt_price_and_keeps_source_literal() -> None:
    body = (
        "sku;name;price;currency;unit;pack\n"
        "A;Valid two decimals;99,50;RUB;шт;1\n"
        "B;Requires review;910.0863200000001;RUB;шт;1\n"
        "C;Grouping;1 250,045 RUB;RUB;шт;1\n"
        "D;Trailing zeros;10.000;RUB;шт;1\n"
        "E;Invalid exponent;1e4;RUB;шт;1\n"
        "F;Invalid negative;-1.250;RUB;шт;1\n"
        "G;Unverified currency;22.123;EUR;шт;1\n"
        "H;Boolean literal;True;RUB;шт;1\n"
    ).encode()
    staged = stage_supplier_prices("vendor.csv", body, MAPPING)
    assert [str(row.price_rub) if row.price_rub is not None else None
            for row in staged.rows] == ["99.50", None, None, None, None, None, None, None]
    assert [row.source_high_precision_price for row in staged.rows] == [
        None, "910.0863200000001", "1 250,045 RUB", "10.000", None, None,
        "22.123", None,
    ]
    assert all(
        "price_precision_requires_review" in staged.rows[i].issues
        and "invalid_or_missing_price" not in staged.rows[i].issues
        for i in (1, 2, 3, 6)
    )
    assert all(
        "invalid_or_missing_price" in staged.rows[i].issues
        for i in (4, 5, 7)
    )
    assert "unconfirmed_or_non_rub_currency" in staged.rows[6].issues
    assert staged.canonical_importable is False


def test_excel_numeric_float_retains_precision_without_rounding() -> None:
    from io import BytesIO

    from openpyxl import Workbook

    book = Workbook()
    ws = book.active
    assert ws is not None
    ws.title = "Supplier"
    ws.append(["sku", "name", "price", "currency", "unit", "pack"])
    ws.append(["B", "Office product", 910.0863200000001, "RUB", "шт", 1])
    output = BytesIO()
    book.save(output)
    book.close()
    stage = stage_supplier_prices(
        "vendor.xlsx", output.getvalue(), replace(MAPPING, sheet="Supplier"),
    )
    assert stage.rows[0].price_rub is None
    assert stage.rows[0].source_high_precision_price is not None
    assert "price_precision_requires_review" in stage.rows[0].issues


def test_source_high_precision_changes_source_row_hash_but_not_normal_price_hash() -> None:
    raw = b"sku;name;price;currency;unit;pack\nA;Item;10.125;RUB;pcs;1\n"
    stage = stage_supplier_prices("vendor.csv", raw, MAPPING)
    row = stage.rows[0]
    assert source_row_sha256(row) != source_row_sha256(
        replace(row, source_high_precision_price="10.126"),
    )
    plain = stage_supplier_prices(
        "vendor.csv", raw.replace(b"10.125", b"10.12"), MAPPING,
    ).rows[0]
    legacy_repr = {
        "line": plain.line, "supplier_sku": plain.supplier_sku, "name": plain.name,
        "price_rub": str(plain.price_rub), "source_category": plain.source_category,
        "source_sale_unit": plain.source_sale_unit,
        "source_pack_size": plain.source_pack_size, "issues": plain.issues,
    }
    legacy_hash = hashlib.sha256(json.dumps(
        legacy_repr, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()
    assert source_row_sha256(plain) == legacy_hash


def test_review_must_supply_exact_rounding_rule_result_and_explanation() -> None:
    content = (
        "sku;name;price;currency;unit;pack\n"
        "A;Office product;10.125;RUB;шт;1\n"
    ).encode()
    original = _decision(content)
    with pytest.raises(CanonicalExportRejected, match="price_precision_review_required"):
        _preview(content, original)
    approved = replace(
        original,
        precision_review=ReviewedPrecisionPrice(
            price_rub=Decimal("10.13"),
            rounding_mode="half_up",
            explanation="Supplier confirmed that amounts are rounded half up to two decimals.",
        ),
    )
    result = _preview(content, approved)
    assert result.validation.publishable
    assert str(result.validation.products[0].price_rub) == "10.13"
    assert result.published is False
    assert result.model_training_authorized is False
    review = approved.precision_review
    assert review is not None
    for corrupted in (
        replace(review, price_rub=Decimal("10.12")),
        replace(review, rounding_mode="invalid"),  # type: ignore[arg-type]
        replace(review, explanation="ok"),
        replace(review, price_rub=Decimal("NaN")),
        replace(review, price_rub=Decimal("100.00")),
    ):
        with pytest.raises(CanonicalExportRejected, match="price_precision"):
            _preview(content, replace(original, precision_review=corrupted))


def test_precision_review_cannot_override_non_rub_or_regular_prices() -> None:
    raw = b"sku;name;price;currency;unit;pack\nA;Item;10.125;EUR;pcs;1\n"
    decision = _decision(raw)
    review = ReviewedPrecisionPrice(
        price_rub=Decimal("10.13"), rounding_mode="half_up",
        explanation="This is a synthetic review test, no exchange rate inferred.",
    )
    with pytest.raises(CanonicalExportRejected, match="currency|precision|price"):
        _preview(raw, replace(decision, precision_review=review))
    regular = raw.replace(b"10.125", b"10.12").replace(b"EUR", b"RUB")
    with pytest.raises(CanonicalExportRejected, match="unexpected_price_precision"):
        _preview(regular, replace(_decision(regular), precision_review=review))


def test_real_ecoplast_prices_require_review_no_catalog_promotion() -> None:
    from openpyxl import load_workbook

    source = (
        Path(__file__).resolve().parents[2]
        / "ml/data/real/research_20261021_supplier_prices/avt_el_ecoplast_20260615.xlsx"
    )
    if not source.exists():
        pytest.skip("Quarantined source original unavailable")
    book = load_workbook(source, read_only=True, data_only=False, keep_links=False)
    try:
        sheet = book.worksheets[0].title
    finally:
        book.close()
    stage = stage_supplier_prices(
        source.name, source.read_bytes(),
        SupplierMapping(sku=0, name=1, price=4, currency=5, sale_unit=2,
                        pieces_per_sale_unit=3, header_row=2, sheet=sheet),
        formula_policy="quarantine_rows",
    )
    assert sum(row.source_high_precision_price is not None for row in stage.rows) > 3000
    assert sum(row.price_rub is not None for row in stage.rows) == 125
    assert stage.canonical_importable is False
    assert stage.model_training_authorized is False


def test_legacy_xls_copy_retains_raw_precision_and_provenance_block() -> None:
    source: list[tuple[int, list[object]]] = [
        (1, ["sku", "name", "price", "currency", "unit", "pack"]),
        (2, ["ORIG-12", "Legacy source", 12.3456, "RUB", "шт", 1]),
    ]
    stage = _stage_source_rows(
        "legacy.xls", b"fake xls test source", MAPPING, source, ImportLimits(),
    )
    row = stage.rows[0]
    assert row.source_high_precision_price == "12.3456"
    assert "price_precision_requires_review" in row.issues
    assert "xls_formula_and_macro_provenance_unverified" in row.issues
    assert stage.canonical_importable is False


def test_malformed_rounding_policy_fails_closed_without_type_error() -> None:
    content = b"sku;name;price;currency;unit;pack\nA;Item;9.995;RUB;pcs;1\n"
    decision = _decision(content)
    invalid_review = ReviewedPrecisionPrice(
        price_rub=Decimal("10.00"),
        rounding_mode=[],  # type: ignore[arg-type]
        explanation="Sufficiently documented rounding policy is required for each source row.",
    )
    with pytest.raises(CanonicalExportRejected, match="price_precision"):
        _preview(content, replace(decision, precision_review=invalid_review))
