"""Bind a reviewed tenant supplier identity to an immutable catalog version.

Requires an authenticated tenant PostgreSQL connection, an approved mapping
profile and either matching original-byte digests or an immutable reviewed
publication with exact mapping proof. Filenames and supplier labels are never
accepted as provenance.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import text

from kpdesk.catalog.supplier_mapping_storage import parse_profile_payload
from kpdesk.catalog.supplier_profiles import ReviewedSupplierProfile, tabular_mapping_from_profile
from kpdesk.catalog.supplier_staging import mapping_digest
from kpdesk.catalog.tenant_storage import TenantCatalogRepository
from kpdesk.quotes.supplier_comparison import VerifiedSupplierCatalog

_SUPPLIER = re.compile(r"[A-Za-z0-9_.-]{1,120}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_LATEST = text("""
    SELECT profile
    FROM public.kpdesk_supplier_mapping_latest(:org,:supplier,:fingerprint)
""")
_VERSION = text("""
    SELECT id,source_sha256,rows_count
    FROM public.catalog_versions
    WHERE organization_id=:org AND id=:version
""")
_BRIDGE_AVAILABLE = text("""
    SELECT to_regprocedure(
      'public.kpdesk_reviewed_catalog_bridge(uuid,uuid,text,text,text,text,text)'
    )
""")
_BRIDGE = text("""
    SELECT public.kpdesk_reviewed_catalog_bridge(
      :org,:version,:supplier,:original,:canonical,:mapping_reject,:mapping_quarantine
    )
""")


@dataclass(frozen=True, slots=True)
class SupplierSourceRef:
    supplier_id: str
    profile_fingerprint: str
    catalog_version_id: uuid.UUID


def load_reviewed_supplier_source(
    catalog: TenantCatalogRepository,
    source: SupplierSourceRef,
) -> VerifiedSupplierCatalog:
    """Fail closed when supplier review, version or source digest is unverifiable."""
    if (_SUPPLIER.fullmatch(source.supplier_id) is None
        or _SHA.fullmatch(source.profile_fingerprint) is None):
        raise ValueError("invalid_supplier_source_selector")
    catalog._authorize("read")
    profile_payload = catalog.connection.scalar(_LATEST, {
        "org": catalog.organization_id,
        "supplier": source.supplier_id,
        "fingerprint": source.profile_fingerprint,
    })
    if profile_payload is None:
        raise ValueError("supplier_review_profile_not_found")
    profile = parse_profile_payload(profile_payload)
    return _verified_profile_catalog(catalog, source, profile)


def _verified_profile_catalog(
    catalog: TenantCatalogRepository,
    source: SupplierSourceRef,
    profile: ReviewedSupplierProfile,
) -> VerifiedSupplierCatalog:
    """Only for a server-authenticated and checksum-verified mapping record.

    Historical audit can use a specific append-only profile; active quote
    creation MUST still call load_reviewed_supplier_source (SQL latest).
    """
    if (profile.organization_id != catalog.organization_id
        or profile.supplier_id != source.supplier_id
        or profile.fingerprint != source.profile_fingerprint):
        raise ValueError("supplier_review_profile_scope_mismatch")
    # Different price bases or VAT assumptions cannot be compared as prices
    # until a reviewed explicit pricing-normalization contract is available.
    if profile.price_basis != "standard" or profile.confirmed_currency != "RUB":
        raise ValueError("supplier_price_basis_not_comparable")
    version = catalog.connection.execute(_VERSION, {
        "org": catalog.organization_id,
        "version": source.catalog_version_id,
    }).one_or_none()
    if version is None:
        raise ValueError("supplier_catalog_version_not_found")
    if profile.approved_source_sha256 != version.source_sha256:
        # The read-only bridge is deliberately a separate, gated migration.
        # Never trust a transformed export merely because a profile exists.
        if catalog.connection.scalar(_BRIDGE_AVAILABLE) is None:
            raise ValueError("supplier_catalog_digest_not_reviewed")
        mapping = tabular_mapping_from_profile(profile)
        attested = catalog.connection.scalar(_BRIDGE, {
            "org": catalog.organization_id,
            "version": source.catalog_version_id,
            "supplier": source.supplier_id,
            "original": profile.approved_source_sha256,
            "canonical": version.source_sha256,
            "mapping_reject": mapping_digest(mapping, "reject"),
            "mapping_quarantine": mapping_digest(mapping, "quarantine_rows"),
        })
        if attested is not True:
            raise ValueError("supplier_catalog_digest_not_reviewed")
    products = tuple(catalog.items(source.catalog_version_id))
    if not products or version.rows_count != len(products):
        raise ValueError("supplier_catalog_row_count_changed")
    return VerifiedSupplierCatalog(
        supplier_id=source.supplier_id,
        supplier_name=source.supplier_id,
        catalog_version_id=source.catalog_version_id,
        source_sha256=version.source_sha256,
        products=products,
    )
