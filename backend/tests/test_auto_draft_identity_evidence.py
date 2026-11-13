"""Guard against semantic substitutions with high fuzzy lexical similarity."""
from __future__ import annotations

from decimal import Decimal

import pytest
from rapidfuzz import fuzz

from kpdesk.matching.schema import CatalogProduct, RequestedItem
from kpdesk.quotes.auto_draft import build_auto_draft


def _product(name: str, *, sku: str = "CAT-001") -> CatalogProduct:
    return CatalogProduct(
        sku=sku, name=name, category="pencil",
        sale_unit="штука", pieces_per_sale_unit=1,
        price_rub=Decimal("29.90"),
        attributes={"pencil_type": "graphite", "lead_grade": "HB"},
    )


def _request(name: str) -> RequestedItem:
    return RequestedItem(
        description=name, category="pencil", quantity=2,
        unit="штука", attributes={"pencil_type": "graphite", "lead_grade": "HB"},
    )


@pytest.mark.parametrize("requested,offered", [
    ("Карандаш графитовый HB корпус чёрный", "Карандаш графитовый HB корпус белый"),
    ("Карандаш графитовый HB корпус красный", "Карандаш графитовый HB корпус синий"),
    ("Карандаш графитовый HB упаковка деревянная", "Карандаш графитовый HB упаковка пластиковая"),
    ("Карандаш графитовый HB без ластика", "Карандаш графитовый HB с ластиком"),
    ("Карандаш графитовый HB тонкий", "Карандаш графитовый HB толстый"),
])
def test_near_identical_but_distinct_item_does_not_auto_select(
    requested: str, offered: str,
) -> None:
    assert fuzz.WRatio(requested, offered) >= 82.0
    result = build_auto_draft([_request(requested)], [_product(offered)])
    assert result.ready_for_draft is False
    assert result.approved is False
    assert result.total_rub is None
    assert result.lines[0].status == "needs_review"
    assert result.lines[0].sku is None
    assert result.lines[0].reason_codes == ("unverified_product_identity",)


def test_exact_name_still_builds_draft_with_exact_rub_total() -> None:
    name = "Карандаш графитовый HB корпус чёрный"
    result = build_auto_draft([_request(name)], [_product(name)])
    assert result.ready_for_draft is True
    assert result.approved is False
    assert result.total_rub == Decimal("59.80")
    assert result.lines[0].sku == "CAT-001"


def test_explicit_supplier_sku_with_different_descriptive_text_still_needs_review() -> None:
    result = build_auto_draft(
        [_request("Карандаш графитовый HB SKU CAT-001 корпус белый")],
        [_product("Карандаш графитовый HB корпус чёрный")],
    )
    assert result.ready_for_draft is False
    assert result.total_rub is None
    assert result.lines[0].sku is None
    assert result.lines[0].reason_codes == ("unverified_product_identity",)
