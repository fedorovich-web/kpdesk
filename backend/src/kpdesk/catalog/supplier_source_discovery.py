"""Bounded server-side discovery of tenant-reviewed supplier catalog versions.

Discovery offers *identifiers*, not trustworthy prices. Preview/create must
revalidate the chosen version against current profile and signed provenance.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text

from kpdesk.catalog.supplier_mapping_storage import (
    TenantSupplierMappingStore,
    parse_profile_payload,
)
from kpdesk.catalog.supplier_profiles import (
    ReviewedSupplierProfile,
    tabular_mapping_from_profile,
)
from kpdesk.catalog.supplier_staging import mapping_digest
from kpdesk.catalog.tenant_storage import TenantCatalogRepository

_SUPPLIER = re.compile(r"[A-Za-z0-9_.-]{1,120}\Z")
_SIGNED_SIGNATURE = "public.kpdesk_reviewed_supplier_versions(uuid,text,integer,integer)"
_SOURCE_VERSIONS = text("""
    SELECT id,source_sha256,rows_count
    FROM public.catalog_versions
    WHERE organization_id=:org AND source_sha256=ANY(:digests)
    ORDER BY created_at DESC,id DESC LIMIT 1001
""")
_SIGNED = text("""
    SELECT catalog_version_id,original_source_sha256,mapping_sha256,
           canonical_sha256,rows_count
    FROM public.kpdesk_reviewed_supplier_versions(:org,:supplier,:limit,:offset)
""")
_AVAILABLE = text("SELECT to_regprocedure(:signature)")
_LATEST_PROFILES = text("""
    SELECT candidate.fingerprint, head.profile
    FROM unnest(CAST(:fingerprints AS text[])) AS candidate(fingerprint)
    CROSS JOIN LATERAL public.kpdesk_supplier_mapping_latest(
        :org,:supplier,candidate.fingerprint
    ) AS head
""")
_MAX_PROFILES = 200
_MAX_SOURCE_ROWS = 1000
_MAX_OPTIONS = 200


@dataclass(frozen=True, slots=True)
class DiscoveredSupplierSource:
    supplier_id: str
    profile_fingerprint: str
    catalog_version_id: uuid.UUID
    source_sha256: str
    rows_count: int
    evidence: Literal["original", "reviewed_canonical"]


@dataclass(frozen=True, slots=True)
class SupplierSourceDiscovery:
    supplier_id: str
    sources: tuple[DiscoveredSupplierSource, ...]
    truncated: bool
    canonical_discovery_available: bool


def _latest_profiles(
    catalog: TenantCatalogRepository, supplier_id: str,
) -> tuple[dict[str, ReviewedSupplierProfile], bool]:
    store = TenantSupplierMappingStore(
        catalog.connection, catalog.identity, catalog.organization_id,
        session_token=catalog.session_token or "",
    )
    observed: dict[str, ReviewedSupplierProfile] = {}
    truncated = False
    for offset in range(0, _MAX_PROFILES, 50):
        records = store.history(supplier_id, limit=50, offset=offset)
        for record in records:
            observed.setdefault(record.profile.fingerprint, record.profile)
        if len(records) < 50:
            break
    else:
        truncated = bool(store.history(supplier_id, limit=1, offset=_MAX_PROFILES))
    if not observed:
        return {}, truncated
    # The existing SQL latest() rejects conflicting mappings of the SAME
    # layout. A history head alone is not approval to use that layout.
    rows = catalog.connection.execute(_LATEST_PROFILES, {
        "org": catalog.organization_id, "supplier": supplier_id,
        "fingerprints": list(observed),
    }).all()
    current: dict[str, ReviewedSupplierProfile] = {}
    for row in rows:
        previous = observed.get(row.fingerprint)
        profile = parse_profile_payload(row.profile)
        if (
            previous is not None
            and profile.profile_sha256 == previous.profile_sha256
            and profile.organization_id == catalog.organization_id
            and profile.supplier_id == supplier_id
            and profile.fingerprint == row.fingerprint
        ):
            current[row.fingerprint] = profile
    return current, truncated


def discover_supplier_sources(
    catalog: TenantCatalogRepository, supplier_id: str,
) -> SupplierSourceDiscovery:
    """One authenticated tenant transaction; no cross-tenant or unsigned fallback."""
    if not isinstance(supplier_id, str) or _SUPPLIER.fullmatch(supplier_id) is None:
        raise ValueError("invalid_supplier_selector")
    catalog._authorize("read")
    profiles, truncated = _latest_profiles(catalog, supplier_id)
    eligible = {
        key: profile for key, profile in profiles.items()
        if profile.price_basis == "standard" and profile.confirmed_currency == "RUB"
    }
    found: dict[tuple[str, uuid.UUID], DiscoveredSupplierSource] = {}

    def record(
        fingerprint: str, version_id: uuid.UUID, digest: str, rows_count: int,
        evidence: Literal["original", "reviewed_canonical"],
    ) -> None:
        nonlocal truncated
        identity = fingerprint, version_id
        if identity in found:
            return
        if len(found) >= _MAX_OPTIONS:
            truncated = True
            return
        found[identity] = DiscoveredSupplierSource(
            supplier_id, fingerprint, version_id, digest, rows_count, evidence,
        )

    # All options are grounded in server-side mapping profiles. Signed
    # canonical outputs are processed first so archival original-byte versions
    # cannot starve them at the explicit response-size limit.
    signed_available = catalog.connection.scalar(_AVAILABLE, {
        "signature": _SIGNED_SIGNATURE,
    }) is not None
    if signed_available and eligible:
        verified: dict[tuple[str, str], list[str]] = {}
        for fingerprint, profile in eligible.items():
            if profile.source_format not in ("csv", "xls", "xlsx"):
                continue
            mapping = tabular_mapping_from_profile(profile)
            for policy in ("reject", "quarantine_rows"):
                key = profile.approved_source_sha256, mapping_digest(mapping, policy)
                verified.setdefault(key, []).append(fingerprint)
        if verified:
            offset = 0
            while offset <= _MAX_SOURCE_ROWS:
                count = min(100, _MAX_SOURCE_ROWS + 1 - offset)
                rows = catalog.connection.execute(_SIGNED, {
                    "org": catalog.organization_id, "supplier": supplier_id,
                    "limit": count, "offset": offset,
                }).all()
                for row in rows:
                    if offset >= _MAX_SOURCE_ROWS:
                        truncated = True
                        break
                    for fingerprint in verified.get((
                        row.original_source_sha256, row.mapping_sha256,
                    ), ()):
                        record(
                            fingerprint, row.catalog_version_id, row.canonical_sha256,
                            row.rows_count, "reviewed_canonical",
                        )
                if len(rows) < count or offset >= _MAX_SOURCE_ROWS:
                    break
                offset += count

    if eligible:
        originals: dict[str, list[str]] = {}
        for fingerprint, profile in eligible.items():
            originals.setdefault(profile.approved_source_sha256, []).append(fingerprint)
        rows = catalog.connection.execute(_SOURCE_VERSIONS, {
            "org": catalog.organization_id, "digests": list(originals),
        }).all()
        truncated |= len(rows) > _MAX_SOURCE_ROWS
        for row in rows[:_MAX_SOURCE_ROWS]:
            for fingerprint in originals.get(row.source_sha256, ()):
                record(fingerprint, row.id, row.source_sha256, row.rows_count, "original")

    return SupplierSourceDiscovery(
        supplier_id=supplier_id,
        sources=tuple(sorted(
            found.values(),
            key=lambda value: (
                value.evidence != "reviewed_canonical",
                str(value.catalog_version_id), value.profile_fingerprint,
            ),
        )),
        truncated=truncated,
        canonical_discovery_available=signed_available,
    )
