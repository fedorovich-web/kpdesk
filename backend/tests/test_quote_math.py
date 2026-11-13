"""Deterministic RUB money and package arithmetic; no invisible quantity rounding."""

from decimal import Decimal

import pytest

from kpdesk.matching.schema import CatalogProduct, RequestedItem
from kpdesk.quotes.calculate import (
    QuoteCalculationError,
    calculate_line,
    calculate_quote,
)


def product(*, sku: str = "P-1", sale_unit: str = "коробка",
            pieces_per_sale_unit: int = 10, price: str = "125.00") -> CatalogProduct:
    return CatalogProduct.model_validate({
        "sku": sku, "name": "Ручка синяя шариковая",
        "category": "pen", "sale_unit": sale_unit,
        "pieces_per_sale_unit": pieces_per_sale_unit,
        "price_rub": price, "pen_ink_color": "синий",
        "pen_ink_type": "шариковая",
    })


def request(count: int | None, unit: str | None) -> RequestedItem:
    return RequestedItem.model_validate({
        "description": "Синяя шариковая ручка",
        "category": "pen", "quantity": count, "unit": unit,
        "pen_ink_color": "синий", "pen_ink_type": "шариковая",
    })


def test_exact_same_unit_quantities_use_catalog_price_and_decimal() -> None:
    result = calculate_line(request(3, "коробка"), product())
    assert result.status == "ready"
    assert result.sale_quantity == 3
    assert result.delivered_pieces == 30
    assert result.price_rub == Decimal("125.00")
    assert result.line_total_rub == Decimal("375.00")
    assert result.requires_explicit_pack_confirmation is False


def test_exact_piece_to_box_conversion_requires_no_rounding() -> None:
    result = calculate_line(request(20, "штука"), product())
    assert result.status == "ready"
    assert result.sale_quantity == 2
    assert result.line_total_rub == Decimal("250.00")
    assert result.overage_pieces == 0


def test_nondivisible_box_quantity_requires_confirmed_sale_quantity() -> None:
    preview = calculate_line(request(11, "штука"), product())
    assert preview.status == "needs_review"
    assert preview.sale_quantity is None
    assert preview.line_total_rub is None
    assert preview.suggested_sale_quantity == 2
    assert "fractional_package" in preview.reason_codes

    approved = calculate_line(
        request(11, "штука"), product(), approved_sale_quantity=2
    )
    assert approved.status == "ready"
    assert approved.sale_quantity == 2
    assert approved.overage_pieces == 9
    assert approved.line_total_rub == Decimal("250.00")
    assert approved.requires_explicit_pack_confirmation is True

    with pytest.raises(QuoteCalculationError, match="approved_sale_quantity"):
        calculate_line(request(11, "штука"), product(), approved_sale_quantity=1)
    with pytest.raises(QuoteCalculationError, match="approved_sale_quantity"):
        calculate_line(request(11, "штука"), product(), approved_sale_quantity=3)


def test_unknown_quantity_or_unit_never_defaults_to_one() -> None:
    for q, unit in ((None, "штука"), (1, None), (None, None)):
        with pytest.raises(QuoteCalculationError, match="missing_quantity_or_unit"):
            calculate_line(request(q, unit), product())


def test_unsupported_pack_unit_mapping_is_needs_review_without_fake_conversion() -> None:
    unknown = product(sale_unit="пачка", pieces_per_sale_unit=1)
    preview = calculate_line(request(11, "штука"), unknown)
    assert preview.status == "needs_review"
    assert preview.sale_quantity is None
    assert "unknown_package_conversion" in preview.reason_codes
    with pytest.raises(QuoteCalculationError, match="incompatible_unit"):
        calculate_line(request(11, "штука"), unknown, approved_sale_quantity=11)


def test_incompatible_critical_attribute_prevents_money_calculation() -> None:
    with pytest.raises(QuoteCalculationError, match="incompatible_product"):
        calculate_line(
            request(10, "штука").model_copy(update={"pen_ink_color": "красный"}),
            product(),
        )
    with pytest.raises(QuoteCalculationError, match="unresolved_attributes"):
        calculate_line(
            request(10, "штука").model_copy(update={"pen_ink_color": None}),
            product(),
        )


def test_strict_decimal_total_and_reviewed_lines_refused() -> None:
    a = calculate_line(request(3, "коробка"), product(price="19.99"))
    b = calculate_line(request(2, "коробка"), product(sku="P-2",price="0.01"))
    total = calculate_quote([a, b])
    assert total.total_rub == Decimal("59.99")
    assert total.line_count == 2
    assert len(total.lines) == 2

    incomplete = calculate_line(request(11, "штука"), product())
    with pytest.raises(QuoteCalculationError, match="unresolved_line"):
        calculate_quote([a, incomplete])


def test_same_sale_unit_approval_quantity_must_equal_requested() -> None:
    with pytest.raises(QuoteCalculationError, match="approved_sale_quantity"):
        calculate_line(request(3, "коробка"), product(), approved_sale_quantity=4)


@pytest.mark.parametrize('approved', [None, 2])
def test_malformed_piece_pack_cannot_enter_calculation_even_with_confirmation(
    approved: int | None,
) -> None:
    malformed = product(sale_unit='штука', pieces_per_sale_unit=12)
    with pytest.raises(QuoteCalculationError, match='inconsistent_piece_package'):
        calculate_line(request(2, 'штука'), malformed, approved_sale_quantity=approved)


def test_one_piece_and_real_box_preserve_the_price_denominator() -> None:
    piece = calculate_line(request(2, 'штука'), product(
        sale_unit='штука', pieces_per_sale_unit=1, price='125.00',
    ))
    box = calculate_line(request(2, 'коробка'), product(
        sale_unit='коробка', pieces_per_sale_unit=12, price='125.00',
    ))
    assert piece.delivered_pieces == 2 and piece.line_total_rub == Decimal('250.00')
    assert box.delivered_pieces == 24 and box.line_total_rub == Decimal('250.00')


@pytest.mark.parametrize('manager_selected', [False, True])
def test_malformed_piece_pack_cannot_produce_a_priced_snapshot(manager_selected: bool) -> None:
    from kpdesk.extraction.schema import ExtractedRequest
    from kpdesk.quotes.line import ManagerSkuSelection
    from kpdesk.quotes.storage import _make_snapshot

    malformed = product(sale_unit='штука', pieces_per_sale_unit=12)
    wanted = request(2, 'штука').model_copy(update={'description': malformed.name})
    extracted = ExtractedRequest.model_validate({'lines': [
        {'source_quote': malformed.name, **wanted.model_dump()},
    ]})
    if manager_selected:
        with pytest.raises(ValueError, match='inconsistent_piece_package'):
            _make_snapshot(extracted, [malformed], [ManagerSkuSelection(
                line_number=1, sku=malformed.sku, approved_sale_quantity=2,
            )])
    else:
        snapshot = _make_snapshot(extracted, [malformed])
        assert snapshot.ready_for_draft is False and snapshot.total_rub is None
        assert snapshot.lines[0].reason_codes == ('inconsistent_piece_package',)
