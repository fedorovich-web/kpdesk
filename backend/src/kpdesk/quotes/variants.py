"""Bounded composition comparisons; neither ML scores nor clients supply money."""
from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from kpdesk.matching.schema import Candidate, RequestedItem, SaleUnit
from kpdesk.quotes.calculate import MAX_RUB, calculate_line
from kpdesk.quotes.instructions import QuoteRules, brand_allowed, selling_total


class VariantLine(BaseModel):
    model_config = ConfigDict(frozen=True)

    line_number: int
    sku: str
    name: str
    sale_quantity: int
    sale_unit: SaleUnit
    line_total_rub: str


class CompositionVariant(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["relevance", "lower_cost"]
    lines: list[VariantLine]
    unresolved_line_numbers: list[int]
    total_rub: str | None
    budget_exceeded: bool
    selectable: bool


def build_variants(
    requests: Sequence[RequestedItem], pools: Sequence[Sequence[Candidate]], rules: QuoteRules,
) -> list[CompositionVariant]:
    """Choose only exactly calculable compatible products within the supplied pools.

    Relevance is a ranking objective, not a calibrated quality guarantee. Lower
    cost minimizes each independent line within the same bounded retrieval pool.
    No availability, discounts or package overage are inferred.
    """
    if not 1 <= len(requests) <= 20 or len(requests) != len(pools):
        raise ValueError("invalid_variant_lines")
    options: list[list[tuple[VariantLine, float, Decimal]]] = []
    for index, (request, candidates) in enumerate(zip(requests, pools, strict=True), 1):
        row: list[tuple[VariantLine, float, Decimal]] = []
        for candidate in candidates:
            product = candidate.product
            if not brand_allowed(product.brand, rules):
                continue
            try:
                calculation = calculate_line(request, product)
                if calculation.line_total_rub is None or calculation.sale_quantity is None:
                    continue
                amount = selling_total(calculation.line_total_rub, rules.markup_percent)
            except ValueError:
                continue
            row.append((VariantLine(
                line_number=index, sku=product.sku, name=product.name,
                sale_quantity=calculation.sale_quantity, sale_unit=product.sale_unit,
                line_total_rub=f"{amount:.2f}",
            ), candidate.score, amount))
        options.append(row)

    variants: list[CompositionVariant] = []
    seen: set[tuple[tuple[int, str, int], ...]] = set()
    for kind in ("relevance", "lower_cost"):
        selected = [min(row, key=(
            (lambda value: (-value[1], value[0].sku)) if kind == "relevance"
            else (lambda value: (value[2], -value[1], value[0].sku))
        )) for row in options if row]
        lines = [value[0] for value in selected]
        identity = tuple((line.line_number, line.sku, line.sale_quantity) for line in lines)
        if identity in seen:
            continue
        seen.add(identity)
        unresolved = [index for index, row in enumerate(options, 1) if not row]
        amount = sum((value[2] for value in selected), Decimal(0))
        total = None if unresolved or amount > MAX_RUB else amount
        exceeded = total is not None and rules.budget_max_rub is not None \
            and total > rules.budget_max_rub
        variants.append(CompositionVariant(
            kind=kind, lines=lines, unresolved_line_numbers=unresolved,
            total_rub=f"{total:.2f}" if total is not None else None,
            budget_exceeded=exceeded, selectable=total is not None and not exceeded,
        ))
    return variants
