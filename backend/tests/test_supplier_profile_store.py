"""Offline tenant-keyed, immutable supplier layout profile store tests."""
from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from kpdesk.catalog.supplier_inference import inspect_supplier_source
from kpdesk.catalog.supplier_profile_store import (
    AmbiguousSupplierProfiles,
    load_local_reviewed_profile,
    save_local_reviewed_profile,
)
from kpdesk.catalog.supplier_profiles import (
    ReviewedSupplierProfile,
    approve_supplier_profile,
    reuse_supplier_profile,
    schema_fingerprint,
)
from kpdesk.db.tenant_access import AuthenticatedUser

ORG = uuid.UUID("30000000-0000-4000-8000-000000000001")
FOREIGN_ORG = uuid.UUID("30000000-0000-4000-8000-000000000002")
REVIEWER = AuthenticatedUser(uuid.UUID("30000000-0000-4000-8000-000000000003"))
ORIGINAL = b"sku;name;price;currency\n000012;Cable;150;RUB\n"


def _new_profile() -> ReviewedSupplierProfile:
    source = inspect_supplier_source("supplier.csv", ORIGINAL)
    return approve_supplier_profile(
        source, organization_id=ORG, supplier_id="supplier_01",
        reviewer=REVIEWER, source_sha256=source.source_sha256,
        selected_columns=source.proposed_fields, price_basis="standard",
        confirmed_currency=None, review_evidence="Original verified by an operator",
        explicitly_confirmed=True,
    )


def test_persist_reload_and_reuse_after_restart(tmp_path: Path) -> None:
    profile = _new_profile()
    location = save_local_reviewed_profile(tmp_path, profile)
    assert location.is_file()
    assert location.stat().st_size < 15_000
    assert save_local_reviewed_profile(tmp_path, profile) == location
    loaded = load_local_reviewed_profile(
        tmp_path, organization_id=ORG, supplier_id="supplier_01",
        fingerprint=profile.fingerprint,
    )
    assert loaded == profile
    refreshed = b"sku;name;price;currency\n000012;Cable;210;RUB\n"
    inspection = inspect_supplier_source("supplier.csv", refreshed)
    assert schema_fingerprint(inspection) == loaded.fingerprint
    result = reuse_supplier_profile(
        loaded, inspection, "supplier.csv", refreshed,
        organization_id=ORG, supplier_id="supplier_01",
    )
    assert str(result.staging.rows[0].price_rub) == "210"
    assert result.canonical_importable is False


def test_profile_is_not_visible_to_other_tenant(tmp_path: Path) -> None:
    profile = _new_profile()
    save_local_reviewed_profile(tmp_path, profile)
    assert load_local_reviewed_profile(
        tmp_path, organization_id=FOREIGN_ORG, supplier_id="supplier_01",
        fingerprint=profile.fingerprint,
    ) is None
    assert load_local_reviewed_profile(
        tmp_path, organization_id=ORG, supplier_id="different_supplier",
        fingerprint=profile.fingerprint,
    ) is None


def test_modified_immutable_profile_is_rejected(tmp_path: Path) -> None:
    profile = _new_profile()
    saved = save_local_reviewed_profile(tmp_path, profile)
    tampered = saved.read_text(encoding="utf-8").replace(
        '"confirmed_currency": null', '"confirmed_currency": "RUB"',
    )
    saved.write_text(tampered, encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        load_local_reviewed_profile(
            tmp_path, organization_id=ORG, supplier_id="supplier_01",
            fingerprint=profile.fingerprint,
        )
    with pytest.raises(ValueError, match="immutable supplier profile"):
        save_local_reviewed_profile(tmp_path, profile)


def test_two_approved_versions_require_review_not_arbitrary_latest(tmp_path: Path) -> None:
    profile = _new_profile()
    save_local_reviewed_profile(tmp_path, profile)
    original_doc = replace(
        profile, review_evidence="Second operator review of same source",
    )
    # A changed review note requires a valid new checksum; use the actual builder.
    changed = approve_supplier_profile(
        inspect_supplier_source("supplier.csv", ORIGINAL),
        organization_id=ORG, supplier_id="supplier_01",
        reviewer=REVIEWER,
        source_sha256=profile.approved_source_sha256,
        selected_columns=dict(profile.columns), price_basis="standard",
        confirmed_currency=None, review_evidence=original_doc.review_evidence,
        explicitly_confirmed=True,
    )
    assert changed.profile_sha256 != profile.profile_sha256
    save_local_reviewed_profile(tmp_path, changed)
    with pytest.raises(AmbiguousSupplierProfiles):
        load_local_reviewed_profile(
            tmp_path, organization_id=ORG, supplier_id="supplier_01",
            fingerprint=profile.fingerprint,
        )


def test_traversal_supplier_path_rejected(tmp_path: Path) -> None:
    valid = _new_profile()
    with pytest.raises(ValueError, match="storage key"):
        save_local_reviewed_profile(tmp_path, replace(valid, supplier_id="../escape"))
