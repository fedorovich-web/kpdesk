"""Verified cross-supplier quote comparison; no self-approval or database writes.

Callers MUST load each source and its digest via tenant-authorized immutable
catalog-version queries. User-provided supplier labels are not provenance.
This module deliberately has no public route until source bindings exist.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from kpdesk.extraction.analysis import product_requirements
from kpdesk.extraction.schema import ExtractedRequest
from kpdesk.extraction.to_matching import ExtractionNeedsReview, to_requested_item
from kpdesk.matching.schema import CatalogProduct, RequestedItem
from kpdesk.quotes.auto_draft import build_auto_draft
from kpdesk.quotes.calculate import MAX_RUB, QuoteCalculationError, calculate_line
from kpdesk.quotes.instructions import (
    ConfirmedInstructions,
    QuoteRules,
    brand_allowed,
    check_confirmation,
    selling_total,
)
from kpdesk.quotes.storage import LockedQuoteLine, QuoteSnapshot, _digest

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_SUPPLIER = re.compile(r"[A-Za-z0-9_.-]{1,120}\Z")


@dataclass(frozen=True, slots=True)
class VerifiedSupplierCatalog:
    """Immutable identity validated by a trusted tenant repository before use."""

    supplier_id: str
    supplier_name: str
    catalog_version_id: uuid.UUID
    source_sha256: str
    products: tuple[CatalogProduct, ...]

    def __post_init__(self) -> None:
        if not _SUPPLIER.fullmatch(self.supplier_id):
            raise ValueError("invalid_supplier_identity")
        if not self.supplier_name.strip() or len(self.supplier_name) > 200:
            raise ValueError("invalid_supplier_name")
        if not _SHA.fullmatch(self.source_sha256):
            raise ValueError("invalid_supplier_source_digest")
        if not self.products or len(self.products) > 5000:
            raise ValueError("invalid_supplier_catalog_size")
        if len({item.sku for item in self.products}) != len(self.products):
            raise ValueError("duplicate_supplier_sku")


@dataclass(frozen=True, slots=True)
class SupplierOffer:
    line_number: int
    supplier_id: str
    supplier_name: str
    catalog_version_id: uuid.UUID
    source_sha256: str
    product: CatalogProduct
    sale_quantity: int
    sale_unit: str
    unit_price_rub: Decimal
    base_total_rub: Decimal
    selling_total_rub: Decimal


@dataclass(frozen=True, slots=True)
class SupplierOfferSelection:
    line_number: int
    supplier_id: str
    catalog_version_id: uuid.UUID
    sku: str


@dataclass(frozen=True, slots=True)
class SupplierComparisonLine:
    line_number: int
    description: str
    offers: tuple[SupplierOffer, ...]
    status: Literal["select_supplier", "no_verified_offer", "request_needs_review"]


@dataclass(frozen=True, slots=True)
class SupplierComparison:
    lines: tuple[SupplierComparisonLine, ...]
    catalog_source_count: int
    request_sha256: str
    instructions_sha256: str
    # No global 'winner' is authorized by merely comparing supplier prices.
    requires_manager_confirmation: bool = True


def _checked_sources(sources: tuple[VerifiedSupplierCatalog, ...]) -> None:
    if not 2 <= len(sources) <= 10:
        raise ValueError("supplier_count_out_of_range")
    if len({source.supplier_id for source in sources}) != len(sources):
        raise ValueError("duplicate_supplier_identity")
    if len({source.catalog_version_id for source in sources}) != len(sources):
        raise ValueError("duplicate_supplier_catalog_version")
    if len({source.source_sha256 for source in sources}) != len(sources):
        raise ValueError("duplicate_supplier_source_content")
    if sum(len(source.products) for source in sources) > 10000:
        raise ValueError("comparison_too_many_skus")


def compare_supplier_catalogs(
    request: ExtractedRequest,
    sources: tuple[VerifiedSupplierCatalog, ...],
    instructions: ConfirmedInstructions | None = None,
) -> SupplierComparison:
    """Only price a verified identity after the existing strict matching gate.

    No tie-breaker chooses a supplier, and a cheaper incompatible SKU never
    displaces a technically verified offer.
    """
    _checked_sources(sources)
    if not 1 <= len(request.lines) <= 20:
        raise ValueError("invalid_quote_line_count")
    rules = check_confirmation(instructions) if instructions is not None else QuoteRules()
    compared: list[SupplierComparisonLine] = []
    for number, source_line in enumerate(request.lines, start=1):
        try:
            wanted: RequestedItem = to_requested_item(source_line)
        except ExtractionNeedsReview:
            compared.append(SupplierComparisonLine(
                number, source_line.description, (), "request_needs_review",
            ))
            continue
        offers: list[SupplierOffer] = []
        for supplier in sources:
            for product in supplier.products:
                if product.category != wanted.category or not brand_allowed(product.brand, rules):
                    continue
                # One candidate is checked by the same identity/pack/attribute
                # gate used by current quote drafts, never by fuzzy ranking.
                draft = build_auto_draft([wanted], [product])
                if draft.lines[0].status != "auto_selected":
                    continue
                try:
                    calculation = calculate_line(wanted, product)
                except QuoteCalculationError:
                    continue
                if calculation.status != "ready" or (
                    calculation.line_total_rub is None
                    or calculation.sale_quantity is None
                ):
                    continue
                offers.append(SupplierOffer(
                    line_number=number,
                    supplier_id=supplier.supplier_id,
                    supplier_name=supplier.supplier_name,
                    catalog_version_id=supplier.catalog_version_id,
                    source_sha256=supplier.source_sha256,
                    product=product,
                    sale_quantity=calculation.sale_quantity,
                    sale_unit=calculation.sale_unit,
                    unit_price_rub=calculation.price_rub,
                    base_total_rub=calculation.line_total_rub,
                    selling_total_rub=selling_total(
                        calculation.line_total_rub, rules.markup_percent,
                    ),
                ))
        # Never return an unbounded candidate matrix to an interactive client.
        if len(offers) > 100:
            raise ValueError("too_many_verified_supplier_offers")
        offers.sort(key=lambda item: (
            item.selling_total_rub, item.supplier_id,
            item.catalog_version_id.hex, item.product.sku,
        ))
        compared.append(SupplierComparisonLine(
            number, source_line.description, tuple(offers),
            "select_supplier" if offers else "no_verified_offer",
        ))
    return SupplierComparison(
        tuple(compared), len(sources),
        _digest(request.model_dump(mode="json")),
        _digest(instructions.model_dump(mode="json") if instructions else None),
    )


def lock_supplier_selection(
    request: ExtractedRequest,
    comparison: SupplierComparison,
    selections: tuple[SupplierOfferSelection, ...],
    instructions: ConfirmedInstructions | None = None,
) -> QuoteSnapshot:
    """Lock exact offer identity and price; selection must be manager-supplied."""
    if (len(comparison.lines) != len(request.lines)
        or comparison.request_sha256 != _digest(request.model_dump(mode="json"))):
        raise ValueError("comparison_request_mismatch")
    if comparison.instructions_sha256 != _digest(
        instructions.model_dump(mode="json") if instructions else None
    ):
        raise ValueError("comparison_instructions_mismatch")
    rules = check_confirmation(instructions) if instructions is not None else QuoteRules()
    chosen: dict[int, SupplierOfferSelection] = {}
    for selection in selections:
        if selection.line_number in chosen or not 1 <= selection.line_number <= len(request.lines):
            raise ValueError("invalid_supplier_selection_line")
        chosen[selection.line_number] = selection

    locked: list[LockedQuoteLine] = []
    selected_offers: list[SupplierOffer] = []
    for line in comparison.lines:
        selection_at_line = chosen.get(line.line_number)
        offer = next((
            item for item in line.offers
            if selection_at_line is not None
            and item.supplier_id == selection_at_line.supplier_id
            and item.catalog_version_id == selection_at_line.catalog_version_id
            and item.product.sku == selection_at_line.sku
        ), None)
        if selection_at_line is not None and offer is None:
            raise ValueError("supplier_selection_not_in_verified_offers")
        request_line = request.lines[line.line_number - 1]
        if offer is None:
            locked.append(LockedQuoteLine(
                line_number=line.line_number,
                description=line.description,
                status="needs_review" if line.status != "no_verified_offer" else "no_match",
                sku=None, product_name=None,
                requested_quantity=request_line.quantity,
                requested_unit=request_line.unit,
                sale_quantity=None, sale_unit=None, unit_price_rub=None,
                line_total_rub=None,
                reason_codes=("supplier_selection_required",)
                if line.status == "select_supplier" else (line.status,),
            ))
            continue
        selected_offers.append(offer)
        locked.append(LockedQuoteLine(
            line_number=line.line_number,
            description=line.description,
            status="manager_selected",
            sku=offer.product.sku,
            product_name=offer.product.name,
            product_brand=offer.product.brand,
            requested_quantity=request_line.quantity,
            requested_unit=request_line.unit,
            sale_quantity=offer.sale_quantity,
            sale_unit=offer.sale_unit,
            unit_price_rub=offer.unit_price_rub,
            line_total_rub=offer.selling_total_rub,
            reason_codes=("supplier_explicitly_selected",),
            supplier_id=offer.supplier_id,
            source_catalog_version_id=offer.catalog_version_id,
            source_sha256=offer.source_sha256,
            requirements=product_requirements(to_requested_item(request_line), offer.product),
        ))
    ready = all(line.status == "manager_selected" for line in locked)
    base = sum((item.base_total_rub for item in selected_offers), Decimal("0.00")) if ready else None
    estimated = sum((
        line.line_total_rub for line in locked if line.line_total_rub is not None
    ), Decimal("0.00")) if ready else None
    if (base is not None and base > MAX_RUB) or (
        estimated is not None and estimated > MAX_RUB
    ):
        raise ValueError("quote_amount_overflow")
    exceeded = (
        estimated is not None and rules.budget_max_rub is not None
        and estimated > rules.budget_max_rub
    )
    return QuoteSnapshot(
        ready_for_draft=ready and not exceeded,
        total_rub=estimated if ready and not exceeded else None,
        lines=tuple(locked), source_request=request,
        instructions=instructions,
        base_total_rub=base,
        estimated_total_rub=estimated,
        budget_exceeded=exceeded,
    )
