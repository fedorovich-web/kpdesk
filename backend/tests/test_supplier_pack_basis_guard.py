"""Supplier price denominator cannot be inferred from absent or conflicting pack evidence."""

from __future__ import annotations

from decimal import Decimal
from io import BytesIO
from uuid import UUID

import pytest
from openpyxl import Workbook

from kpdesk.catalog.supplier_canonical_preview import (
    CanonicalExportRejected,
    CanonicalPreview,
    CanonicalUnit,
    ReviewedProductDecision,
    _price_unit_basis_needs_review,
    preview_canonical_catalog,
    source_row_sha256,
)
from kpdesk.catalog.supplier_staging import SupplierMapping, stage_supplier_prices
from kpdesk.db.tenant_access import AuthenticatedUser

_ORG = UUID("60000000-0000-4000-8000-000000000001")
_REVIEWER = AuthenticatedUser(UUID("60000000-0000-4000-8000-000000000002"))
_EVIDENCE = (
    "Reviewed original supplier tariff and independently confirmed that "
    "250.00 RUB is quoted for the approved sale unit, without price division."
)


@pytest.mark.parametrize(("source_unit", "source_pack", "canonical_unit", "canonical_pack", "requires_review"), [
    ("", "", "штука", 1, True),
    ("", "1", "штука", 1, True),
    ("", "12", "пачка", 12, True),
    ("шт", "", "штука", 1, True),
    ("шт", "1", "штука", 1, False),
    ("шт", "12", "штука", 1, True),
    ("уп", "", "пачка", 12, True),
    ("уп", "12", "пачка", 12, False),
    ("уп", "12", "пачка", 1, True),
    ("уп", "12", "штука", 1, True),
    ("уп", "12", "коробка", 12, True),
    ("уп", "0", "пачка", 12, True),
])
def test_price_denominator_requires_source_evidence_or_explicit_review(
    source_unit: str,
    source_pack: str,
    canonical_unit: CanonicalUnit,
    canonical_pack: int,
    requires_review: bool,
) -> None:
    source = (
        "sku;name;price;currency;unit;pack\n"
        f"ITEM-1;Supplier product;250.00;RUB;{source_unit};{source_pack}\n"
    ).encode()
    staging = stage_supplier_prices(
        "supplier.csv", source,
        SupplierMapping(
            name=1, price=2, header_row=1, sku=0, currency=3,
            sale_unit=4, pieces_per_sale_unit=5,
        ),
    )
    row = staging.rows[0]
    decision = ReviewedProductDecision(
        line=row.line, category="stationery_other", sale_unit=canonical_unit,
        pieces_per_sale_unit=canonical_pack, source_row_sha256=source_row_sha256(row),
        confirmed=True, attributes={"product_kind": "item"},
    )
    assert _price_unit_basis_needs_review(row, decision) is requires_review
    def render(review: ReviewedProductDecision) -> CanonicalPreview:
        return preview_canonical_catalog(
            staging, (review,), organization_id=_ORG, reviewer=_REVIEWER,
            expected_source_sha256=staging.sha256, price_basis="standard",
            explicitly_confirmed_price_basis=True,
        )

    if requires_review:
        with pytest.raises(CanonicalExportRejected, match="price_unit_basis_requires_review"):
            render(decision)
        decision = ReviewedProductDecision(
            line=decision.line, category=decision.category,
            sale_unit=decision.sale_unit, pieces_per_sale_unit=decision.pieces_per_sale_unit,
            source_row_sha256=decision.source_row_sha256,
            confirmed=True, attributes=decision.attributes,
            price_unit_review_evidence=_EVIDENCE,
        )
    result = render(decision)
    assert result.validation.publishable
    assert result.validation.products[0].price_rub == Decimal("250.00")
    assert result.validation.products[0].sale_unit == canonical_unit
    assert result.validation.products[0].pieces_per_sale_unit == canonical_pack
    assert result.model_training_authorized is False
    assert result.published is False


@pytest.mark.parametrize(("original_unit", "pack_cell", "approved_unit", "approved_pack", "review_required"), [
    (None, 1, "штука", 1, True),
    ("шт", 12, "штука", 1, True),
    ("уп", 12, "пачка", 12, False),
])
def test_native_xlsx_numeric_pack_cannot_override_price_denominator(
    original_unit: str | None, pack_cell: int,
    approved_unit: CanonicalUnit, approved_pack: int, review_required: bool,
) -> None:
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "Vendor"
    sheet.append(["sku", "name", "price", "currency", "unit", "pack"])
    sheet.append(["SKU-001", "Commercial test fixture", 250.0, "RUB", original_unit, pack_cell])
    buffer = BytesIO()
    book.save(buffer)
    book.close()
    staged = stage_supplier_prices(
        "vendor.xlsx", buffer.getvalue(),
        SupplierMapping(
            name=1, price=2, header_row=1, sku=0,
            currency=3, sale_unit=4, pieces_per_sale_unit=5, sheet="Vendor",
        ),
    )
    row = staged.rows[0]
    assert row.source_pack_size == pack_cell
    assert row.price_rub == Decimal(250)
    decision = ReviewedProductDecision(
        line=row.line, category="stationery_other", sale_unit=approved_unit,
        pieces_per_sale_unit=approved_pack, source_row_sha256=source_row_sha256(row),
        confirmed=True, attributes={"product_kind": "item"},
    )
    assert _price_unit_basis_needs_review(row, decision) is review_required
    assert staged.canonical_importable is False
    assert staged.model_training_authorized is False


@pytest.mark.parametrize(("source_pack", "issue"), [
    ("", "missing_pack_size"),
    ("0", "invalid_pack_size"),
    ("5.7", "invalid_pack_size"),
])
def test_bad_mapped_pack_cannot_bypass_unit_price_review_for_pieces(
    source_pack: str, issue: str,
) -> None:
    content = (
        "sku;name;price;currency;unit;pack\n"
        f"K-0001;USB adapter;250.00;RUB;pcs;{source_pack}\n"
    ).encode()
    stage = stage_supplier_prices(
        "supplier.csv", content,
        SupplierMapping(sku=0, name=1, price=2, currency=3,
                        sale_unit=4, pieces_per_sale_unit=5, header_row=1),
    )
    row = stage.rows[0]
    assert issue in row.issues
    decision = ReviewedProductDecision(
        line=row.line, category="stationery_other",
        sale_unit="штука", pieces_per_sale_unit=1,
        source_row_sha256=source_row_sha256(row),
        confirmed=True, attributes={"product_kind": "item"},
    )
    assert _price_unit_basis_needs_review(row, decision)
    with pytest.raises(CanonicalExportRejected, match="price_unit_basis_requires_review"):
        preview_canonical_catalog(
            stage, (decision,), organization_id=_ORG, reviewer=_REVIEWER,
            expected_source_sha256=stage.sha256,
            price_basis="standard", explicitly_confirmed_price_basis=True,
        )
