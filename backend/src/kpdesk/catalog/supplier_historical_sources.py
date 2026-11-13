"""Read-only supplier source history independent of live quote eligibility.

An immutable mapping *record checksum*, not merely its layout fingerprint,
binds a past catalog version to a specific review. Historical records never
flow into source discovery for fresh quote preview/creation.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text

from kpdesk.catalog.supplier_catalog_diff import (
    SupplierCatalogDiff,
    _diff_verified_catalogs,
)
from kpdesk.catalog.supplier_mapping_storage import TenantSupplierMappingStore
from kpdesk.catalog.supplier_profiles import ReviewedSupplierProfile
from kpdesk.catalog.tenant_storage import TenantCatalogRepository
from kpdesk.catalog.verified_supplier_sources import (
    SupplierSourceRef,
    _verified_profile_catalog,
)
from kpdesk.quotes.supplier_comparison import VerifiedSupplierCatalog

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_SUPPLIER = re.compile(r"[A-Za-z0-9_.-]{1,120}\Z")
_VERSIONS = text("""
    SELECT id,source_sha256,rows_count FROM public.catalog_versions
    WHERE organization_id=:org AND source_sha256=ANY(:digests)
    ORDER BY created_at DESC,id DESC LIMIT 1001
""")
_MAX_HISTORY = 500
_MAX_RESULTS = 200


@dataclass(frozen=True, slots=True)
class HistoricalSupplierRef:
    supplier_id: str
    profile_sha256: str
    catalog_version_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class HistoricalSupplierSource:
    supplier_id: str
    profile_sha256: str
    profile_fingerprint: str
    catalog_version_id: uuid.UUID
    source_sha256: str
    rows_count: int
    evidence: Literal["original"] = "original"


@dataclass(frozen=True, slots=True)
class HistoricalSupplierSources:
    supplier_id: str
    options: tuple[HistoricalSupplierSource, ...]
    truncated: bool
    audit_only: Literal[True] = True


def _history(
    catalog: TenantCatalogRepository, supplier: str,
) -> tuple[tuple[ReviewedSupplierProfile, ...], bool]:
    if not isinstance(supplier, str) or _SUPPLIER.fullmatch(supplier) is None:
        raise ValueError("invalid_historical_supplier")
    catalog._authorize("read")
    store = TenantSupplierMappingStore(
        catalog.connection, catalog.identity, catalog.organization_id,
        session_token=catalog.session_token or "",
    )
    profiles: list[ReviewedSupplierProfile] = []
    for offset in range(0, _MAX_HISTORY, 50):
        page = store.history(supplier, limit=50, offset=offset)
        profiles.extend(record.profile for record in page)
        if len(page) < 50:
            return tuple(profiles), False
    # With 500 rows visible, there may be more: fail transparently rather than
    # asserting completeness without a database-supported next page.
    return tuple(profiles), True


def _eligible(profile: ReviewedSupplierProfile) -> bool:
    return profile.price_basis == "standard" and profile.confirmed_currency == "RUB"


def _sources(
    catalog: TenantCatalogRepository, supplier: str,
) -> HistoricalSupplierSources:
    profiles, truncated = _history(catalog, supplier)
    eligible = [profile for profile in profiles if _eligible(profile)]
    digests = sorted({profile.approved_source_sha256 for profile in eligible})
    if not digests:
        return HistoricalSupplierSources(supplier, (), truncated)
    rows = catalog.connection.execute(_VERSIONS, {
        "org": catalog.organization_id, "digests": digests,
    }).all()
    if len(rows) > 1000:
        truncated = True
    per_digest: dict[str, list[ReviewedSupplierProfile]] = {}
    for profile in eligible:
        per_digest.setdefault(profile.approved_source_sha256, []).append(profile)
    found: dict[tuple[str, uuid.UUID], HistoricalSupplierSource] = {}
    for row in rows[:1000]:
        for profile in per_digest.get(row.source_sha256, ()):
            key = profile.profile_sha256, row.id
            if key in found:
                continue
            if len(found) >= _MAX_RESULTS:
                truncated = True
                continue
            found[key] = HistoricalSupplierSource(
                supplier_id=supplier, profile_sha256=profile.profile_sha256,
                profile_fingerprint=profile.fingerprint,
                catalog_version_id=row.id, source_sha256=row.source_sha256,
                rows_count=row.rows_count,
            )
    return HistoricalSupplierSources(
        supplier_id=supplier, options=tuple(found.values()), truncated=truncated,
    )


def discover_historical_supplier_sources(
    catalog: TenantCatalogRepository, supplier: str,
) -> HistoricalSupplierSources:
    """Original bytes only, including superseded layouts, with exact audit checksums.

    Signed canonical versions remain discoverable through the existing signed
    provenance path, not misrepresented as original-byte publications here.
    """
    return _sources(catalog, supplier)


def _load_historical_source(
    catalog: TenantCatalogRepository,
    ref: HistoricalSupplierRef,
) -> VerifiedSupplierCatalog:
    if (
        not isinstance(ref.supplier_id, str)
        or _SUPPLIER.fullmatch(ref.supplier_id) is None
        or not isinstance(ref.profile_sha256, str)
        or _SHA.fullmatch(ref.profile_sha256) is None
        or not isinstance(ref.catalog_version_id, uuid.UUID)
    ):
        raise ValueError("invalid_historical_source_selector")
    profiles, truncated = _history(catalog, ref.supplier_id)
    matching = [profile for profile in profiles
                if profile.profile_sha256 == ref.profile_sha256]
    if len(matching) != 1:
        if truncated:
            raise ValueError("historical_profile_beyond_bounded_history")
        raise ValueError("historical_review_not_found")
    profile = matching[0]
    if not _eligible(profile):
        raise ValueError("supplier_price_basis_not_comparable")
    return _verified_profile_catalog(
        catalog, SupplierSourceRef(
            ref.supplier_id, profile.fingerprint, ref.catalog_version_id,
        ), profile,
    )


def compare_historical_supplier_sources(
    catalog: TenantCatalogRepository,
    before: HistoricalSupplierRef,
    after: HistoricalSupplierRef,
    *, offset: int = 0, limit: int = 50,
) -> SupplierCatalogDiff:
    if before.supplier_id != after.supplier_id:
        raise ValueError("source_diff_cross_supplier")
    if before.catalog_version_id == after.catalog_version_id:
        raise ValueError("source_diff_same_version")
    if not 0 <= offset <= 10000 or not 1 <= limit <= 100:
        raise ValueError("source_diff_invalid_page")
    previous = _load_historical_source(catalog, before)
    updated = _load_historical_source(catalog, after)
    return _diff_verified_catalogs(previous, updated, offset=offset, limit=limit)
