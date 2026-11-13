"""Exact price calculations for verified catalog items, with no silent pack rounding.

This is pure domain logic: no LLM output is a price; no tax, delivery or
discount is inferred. A manager must explicitly confirm any package overage.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from kpdesk.matching.constraints import check_constraints
from kpdesk.matching.schema import CatalogProduct, RequestedItem, SaleUnit

MAX_RUB = Decimal("999999999999.99")
CENT = Decimal("0.01")
type LineStatus = Literal["ready", "needs_review"]


class QuoteCalculationError(ValueError):
    """Stable codes for domain errors; never assumes a missing detail."""


@dataclass(frozen=True, slots=True)
class LineCalculation:
    sku: str
    status: LineStatus
    requested_quantity: int
    requested_unit: SaleUnit
    sale_unit: SaleUnit
    pieces_per_sale_unit: int
    price_rub: Decimal
    sale_quantity: int | None
    suggested_sale_quantity: int | None
    delivered_pieces: int | None
    overage_pieces: int | None
    line_total_rub: Decimal | None
    requires_explicit_pack_confirmation: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QuoteTotal:
    lines: tuple[LineCalculation, ...]
    total_rub: Decimal
    line_count: int


def _unresolved_line(
    product: CatalogProduct,
    quantity: int,
    unit: SaleUnit,
    reason: str,
    suggested: int | None = None,
) -> LineCalculation:
    return LineCalculation(
        sku=product.sku, status="needs_review",
        requested_quantity=quantity, requested_unit=unit,
        sale_unit=product.sale_unit,
        pieces_per_sale_unit=product.pieces_per_sale_unit,
        price_rub=product.price_rub,
        sale_quantity=None, suggested_sale_quantity=suggested,
        delivered_pieces=None, overage_pieces=None,
        line_total_rub=None, requires_explicit_pack_confirmation=False,
        reason_codes=(reason,),
    )


def calculate_line(
    request: RequestedItem,
    product: CatalogProduct,
    approved_sale_quantity: int | None = None,
) -> LineCalculation:
    """Price only an explicitly compatible item; distinguish exact vs fractional packs."""
    count = request.quantity
    unit = request.unit
    if count is None or unit is None:
        raise QuoteCalculationError("missing_quantity_or_unit")
    if type(count) is not int or count < 1:
        raise QuoteCalculationError("invalid_quantity")
    if approved_sale_quantity is not None and (
        type(approved_sale_quantity) is not int or approved_sale_quantity < 1
    ):
        raise QuoteCalculationError("invalid_approved_sale_quantity")

    decision = check_constraints(request, product)
    if decision.status == "incompatible":
        raise QuoteCalculationError("incompatible_product")
    if decision.status == "needs_review":
        raise QuoteCalculationError("unresolved_attributes")

    pack_size = product.pieces_per_sale_unit
    if product.sale_unit == "штука" and pack_size != 1:
        raise QuoteCalculationError("inconsistent_piece_package")
    if unit == product.sale_unit:
        sale_count = count
        overage = 0
        confirmation = False
    elif (unit == "штука"
          and product.sale_unit in ("коробка", "пачка")
          and pack_size > 1):
        sale_count, remainder = divmod(count, pack_size)
        if remainder:
            sale_count += 1
            if approved_sale_quantity is None:
                return _unresolved_line(
                    product, count, unit, "fractional_package", sale_count
                )
            overage = sale_count * pack_size - count
            confirmation = True
        else:
            overage = 0
            confirmation = False
    else:
        if approved_sale_quantity is not None:
            raise QuoteCalculationError("incompatible_unit")
        return _unresolved_line(product, count, unit, "unknown_package_conversion")

    if approved_sale_quantity is not None and approved_sale_quantity != sale_count:
        raise QuoteCalculationError("approved_sale_quantity_mismatch")
    amount = product.price_rub * sale_count
    if amount > MAX_RUB or amount != amount.quantize(CENT):
        raise QuoteCalculationError("money_overflow")
    return LineCalculation(
        sku=product.sku, status="ready",
        requested_quantity=count,
        requested_unit=unit,
        sale_unit=product.sale_unit,
        pieces_per_sale_unit=pack_size,
        price_rub=product.price_rub,
        sale_quantity=sale_count,
        suggested_sale_quantity=sale_count,
        delivered_pieces=sale_count * pack_size,
        overage_pieces=overage,
        line_total_rub=amount.quantize(CENT),
        requires_explicit_pack_confirmation=confirmation,
        reason_codes=("explicit_package_overage_confirmed",) if confirmation else (),
    )


def calculate_quote(lines: list[LineCalculation] | tuple[LineCalculation, ...]) -> QuoteTotal:
    """A quote cannot have incomplete lines or unspecified totals."""
    if not lines:
        raise QuoteCalculationError("empty_quote")
    if len(lines) > 20:
        raise QuoteCalculationError("too_many_lines")
    if any(line.status != "ready" or line.line_total_rub is None for line in lines):
        raise QuoteCalculationError("unresolved_line")
    total = sum((line.line_total_rub for line in lines if line.line_total_rub is not None), Decimal(0))
    if total > MAX_RUB:
        raise QuoteCalculationError("money_overflow")
    return QuoteTotal(
        lines=tuple(lines),
        total_rub=total.quantize(CENT),
        line_count=len(lines),
    )
