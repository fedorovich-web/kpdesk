"""Source-verified immutable supplier catalog diff; never auto-approves repricing.

Both versions pass the existing tenant/source/mapping/signed-provenance gate.
SKU identity is exact. A changed product specification is deliberately *not*
treated as a comparable price adjustment, even when its SKU is unchanged.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from kpdesk.catalog.tenant_storage import TenantCatalogRepository
from kpdesk.catalog.verified_supplier_sources import (
    SupplierSourceRef,
    load_reviewed_supplier_source,
)
from kpdesk.matching.schema import CatalogProduct
from kpdesk.quotes.supplier_comparison import VerifiedSupplierCatalog

ChangeKind = Literal[
    "added", "removed", "price_changed", "product_changed",
    "product_and_price_changed",
]
_KINDS: tuple[ChangeKind, ...] = (
    "added", "removed", "price_changed", "product_changed",
    "product_and_price_changed",
)
_FIELDS = (
    "name", "category", "sale_unit", "pieces_per_sale_unit", "brand",
    "paper_format", "paper_gsm", "pen_ink_color", "pen_ink_type", "attributes",
)


@dataclass(frozen=True, slots=True)
class SupplierCatalogChange:
    sku: str
    kind: ChangeKind
    changed_fields: tuple[str, ...]
    before_price_rub: Decimal | None
    after_price_rub: Decimal | None
    comparable_price_delta_rub: Decimal | None


@dataclass(frozen=True, slots=True)
class SupplierCatalogDiff:
    supplier_id: str
    before_catalog_version_id: uuid.UUID
    after_catalog_version_id: uuid.UUID
    before_source_sha256: str
    after_source_sha256: str
    counts: dict[str, int]
    total_changes: int
    offset: int
    next_offset: int | None
    changes: tuple[SupplierCatalogChange, ...]
    requires_manager_review: bool = True


def _changed_spec(before: CatalogProduct, after: CatalogProduct) -> tuple[str, ...]:
    return tuple(
        field for field in _FIELDS
        if getattr(before, field) != getattr(after, field)
    )


def supplier_catalog_diff(
    catalog: TenantCatalogRepository,
    before: SupplierSourceRef,
    after: SupplierSourceRef,
    *, offset: int = 0,
    limit: int = 50,
) -> SupplierCatalogDiff:
    """Diff only two independently reverified versions of ONE supplier."""
    if before.supplier_id != after.supplier_id:
        raise ValueError("source_diff_cross_supplier")
    if before.catalog_version_id == after.catalog_version_id:
        raise ValueError("source_diff_same_version")
    if not 0 <= offset <= 10000 or not 1 <= limit <= 100:
        raise ValueError("source_diff_invalid_page")
    previous = load_reviewed_supplier_source(catalog, before)
    updated = load_reviewed_supplier_source(catalog, after)
    return _diff_verified_catalogs(previous, updated, offset=offset, limit=limit)


def _diff_verified_catalogs(
    previous: VerifiedSupplierCatalog,
    updated: VerifiedSupplierCatalog,
    *, offset: int, limit: int,
) -> SupplierCatalogDiff:
    """Shared pure comparison; each entry must first pass its own provenance gate."""
    if previous.supplier_id != updated.supplier_id:
        raise ValueError("source_diff_cross_supplier")
    if previous.catalog_version_id == updated.catalog_version_id:
        raise ValueError("source_diff_same_version")
    old = {product.sku: product for product in previous.products}
    new = {product.sku: product for product in updated.products}
    counts: dict[str, int] = {kind: 0 for kind in _KINDS}
    # At most 10,000 SKUs: each source is bounded by the verified catalog
    # contract (5,000). Never return or persist an unbounded change list.
    changes: list[SupplierCatalogChange] = []
    for sku in sorted(old.keys() | new.keys()):
        earlier = old.get(sku)
        later = new.get(sku)
        changed_fields: tuple[str, ...]
        if earlier is None:
            kind: ChangeKind = "added"
            changed_fields = ()
        elif later is None:
            kind = "removed"
            changed_fields = ()
        else:
            changed_fields = _changed_spec(earlier, later)
            price_changed = earlier.price_rub != later.price_rub
            if not changed_fields and not price_changed:
                continue
            if changed_fields:
                kind = "product_and_price_changed" if price_changed else "product_changed"
            else:
                kind = "price_changed"
        counts[kind] += 1
        ordinal = sum(counts.values()) - 1
        if offset <= ordinal < offset + limit:
            changes.append(SupplierCatalogChange(
                sku=sku, kind=kind, changed_fields=changed_fields,
                before_price_rub=earlier.price_rub if earlier else None,
                after_price_rub=later.price_rub if later else None,
                comparable_price_delta_rub=(
                    later.price_rub - earlier.price_rub
                    if earlier and later and not changed_fields else None
                ),
            ))
    total = sum(counts.values())
    return SupplierCatalogDiff(
        supplier_id=previous.supplier_id,
        before_catalog_version_id=previous.catalog_version_id,
        after_catalog_version_id=updated.catalog_version_id,
        before_source_sha256=previous.source_sha256,
        after_source_sha256=updated.source_sha256,
        counts=counts,
        total_changes=total,
        offset=offset,
        next_offset=offset + limit if total > offset + limit else None,
        changes=tuple(changes),
    )
