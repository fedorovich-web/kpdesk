from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from kpdesk.extraction.schema import ExtractedRequest
from kpdesk.matching.schema import CatalogProduct
from kpdesk.quotes.pdf import render_draft_pdf
from kpdesk.quotes.storage import (
    ManagerSkuSelection,
    StoredQuote,
    _hash_request,
    _make_snapshot,
)


def request(quantity: int = 2, unit: str = "пачка") -> ExtractedRequest:
    return ExtractedRequest.model_validate({"lines": [{
        "source_quote": "Бумага А4 80 г",
        "description": "Бумага А4 80 г",
        "category": "paper",
        "quantity": quantity,
        "unit": unit,
        "brand": None,
        "paper_format": "A4",
        "paper_gsm": 80,
        "pen_ink_color": None,
        "pen_ink_type": None,
        "attributes": {},
    }]})


def products() -> list[CatalogProduct]:
    return [
        CatalogProduct(
            sku=sku, name="Бумага А4 80 г " + sku, category="paper",
            sale_unit="пачка", pieces_per_sale_unit=500,
            price_rub=Decimal(price), paper_format="A4", paper_gsm=80,
        ) for sku, price in (("SKU-A", "249.50"), ("SKU-B", "300.00"))
    ]


def select(sku: str, quantity: int | None = None) -> ManagerSkuSelection:
    return ManagerSkuSelection(
        line_number=1, sku=sku, approved_sale_quantity=quantity,
    )


def test_manager_picks_precisely_priced_sku_without_auto_approval() -> None:
    original = _make_snapshot(request(), products())
    assert not original.ready_for_draft
    assert original.lines[0].status == "needs_review"

    snapshot = _make_snapshot(request(), products(), [select("SKU-B")])
    assert snapshot.ready_for_draft
    assert snapshot.total_rub == Decimal("600.00")
    line = snapshot.lines[0]
    assert line.status == "manager_selected"
    assert line.sku == "SKU-B"
    assert line.unit_price_rub == Decimal("300.00")
    assert line.sale_quantity == 2
    quote = StoredQuote(
        quote_id=uuid.uuid4(), revision=1, catalog_version_id=uuid.uuid4(),
        ready_for_draft=True, total_rub=snapshot.total_rub,
        lines=snapshot.lines, snapshot_sha256="0" * 64,
    )
    pdf = render_draft_pdf(quote)
    assert pdf.startswith(b"%PDF-")


def test_selection_cannot_bypass_missing_or_wrong_sku() -> None:
    for sku in ("FOREIGN-SKU", "SKU-C"):
        with pytest.raises(ValueError, match="Selected SKU is missing"):
            _make_snapshot(request(), products(), [select(sku)])
    invalid = CatalogProduct(
        sku="WRONG", name="Красная ручка", category="pen", sale_unit="штука",
        pieces_per_sale_unit=1, price_rub=Decimal("5.00"),
    )
    with pytest.raises(ValueError, match="incompatible_product"):
        _make_snapshot(request(), [*products(), invalid], [select("WRONG")])


def test_package_conversion_requires_explicit_quantity() -> None:
    with pytest.raises(ValueError, match="unresolved price or packaging"):
        _make_snapshot(request(600, "штука"), products(), [select("SKU-A")])
    with pytest.raises(ValueError, match="approved_sale_quantity_mismatch"):
        _make_snapshot(request(600, "штука"), products(), [select("SKU-A", 3)])
    result = _make_snapshot(request(600, "штука"), products(), [select("SKU-A", 2)])
    assert result.total_rub == Decimal("499.00")
    assert result.lines[0].sale_quantity == 2
    assert result.lines[0].reason_codes == ("explicit_package_overage_confirmed",)


def test_duplicate_and_out_of_range_selection_are_rejected() -> None:
    with pytest.raises(ValueError, match="line identity"):
        _make_snapshot(request(), products(), [select("SKU-A"), select("SKU-B")])
    with pytest.raises(ValueError, match="line identity"):
        _make_snapshot(request(), products(), [
            ManagerSkuSelection(line_number=2, sku="SKU-A"),
        ])


def test_idempotency_hash_binds_manual_selection_but_not_order() -> None:
    extracted = request()
    catalog = uuid.uuid4()
    standard = _hash_request(catalog, extracted, None, None)
    first = _hash_request(catalog, extracted, None, None, [select("SKU-A")])
    second = _hash_request(catalog, extracted, None, None, [select("SKU-B")])
    assert first != standard
    assert second != first
    assert _hash_request(catalog, extracted, None, None) == standard
