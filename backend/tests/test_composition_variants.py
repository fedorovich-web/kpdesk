"""Real variants share hard constraints and exact quote arithmetic."""
from decimal import Decimal

from kpdesk.matching.constraints import check_constraints
from kpdesk.matching.schema import Candidate, CatalogProduct, RequestedItem
from kpdesk.quotes.instructions import QuoteRules
from kpdesk.quotes.variants import build_variants


def item(quantity: int = 10) -> RequestedItem:
    return RequestedItem(description="Карандаш HB", category="pencil", quantity=quantity,
                         unit="штука", attributes={"pencil_type": "graphite", "lead_grade": "HB"})


def candidate(sku: str, price: str, score: float, *, pack: int = 1,
              brand: str = "Demo", grade: str = "HB") -> Candidate:
    product = CatalogProduct(
        sku=sku, name="Карандаш HB", category="pencil", price_rub=Decimal(price),
        sale_unit="штука" if pack == 1 else "коробка", pieces_per_sale_unit=pack,
        brand=brand, attributes={"pencil_type": "graphite", "lead_grade": grade},
    )
    return Candidate(product, check_constraints(item(), product), score)


def test_variants_compare_normalized_pack_amounts_with_line_markup() -> None:
    result = build_variants([item()], [[candidate("BEST", "10.01", .9),
                                       candidate("PACK", "30.01", .5, pack=5)]],
                            QuoteRules(markup_percent=Decimal(10)))
    assert [v.kind for v in result] == ["relevance", "lower_cost"]
    assert [v.total_rub for v in result] == ["110.11", "66.02"]
    assert result[1].lines[0].sale_quantity == 2
    assert result[1].lines[0].sku == "PACK"


def test_deduplicates_and_does_not_round_partial_packs_or_invent_missing_rows() -> None:
    result = build_variants([item(3), item()], [
        [candidate("PARTIAL", "1.00", .99, pack=5), candidate("EXACT", "2.00", .8)], [],
    ], QuoteRules())
    assert len(result) == 1
    assert result[0].lines[0].sku == "EXACT"
    assert result[0].unresolved_line_numbers == [2]
    assert result[0].total_rub is None
    assert not result[0].selectable


def test_budget_and_brands_and_incompatible_attributes_remain_hard_gates() -> None:
    result = build_variants([item()], [[
        candidate("DENIED", "0.01", 1, brand="Denied"),
        candidate("WRONG", "0.02", .99, grade="2B"),
        candidate("OK", "10.00", .8),
    ]], QuoteRules(excluded_brands=("Denied",), budget_max_rub=Decimal("99.99")))
    assert len(result) == 1
    assert result[0].lines[0].sku == "OK"
    assert result[0].budget_exceeded and not result[0].selectable
    assert result[0].total_rub == "100.00"


def test_ties_are_deterministic_and_overflow_is_unresolved() -> None:
    result = build_variants([item()], [[candidate("B", "1.00", .8),
                                       candidate("A", "1.00", .8)]], QuoteRules())
    assert len(result) == 1 and result[0].lines[0].sku == "A"
    overflow = build_variants([item()], [[candidate("HUGE", "999999999999.99", .8)]],
                              QuoteRules())
    assert overflow[0].total_rub is None and not overflow[0].selectable


def test_total_overflow_and_missing_quantity_never_become_a_complete_offer() -> None:
    huge = candidate("HUGE", "600000000000.00", .8)
    result = build_variants([item(1), item(1)], [[huge], [huge]], QuoteRules())
    assert result[0].total_rub is None and not result[0].selectable
    incomplete = item().model_copy(update={"quantity": None})
    result = build_variants([incomplete], [[candidate("A", "1.00", .8)]], QuoteRules())
    assert result[0].unresolved_line_numbers == [1] and not result[0].selectable
