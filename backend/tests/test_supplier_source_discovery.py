"""Fail-closed source discovery: latest mapping approval and signed provenance."""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import Mock, patch

from kpdesk.catalog.supplier_source_discovery import (
    _latest_profiles,
    discover_supplier_sources,
)
from kpdesk.catalog.supplier_staging import SupplierMapping, mapping_digest


def _fixtures() -> tuple[Mock, SimpleNamespace, SimpleNamespace]:
    catalog = Mock()
    catalog.organization_id = uuid.uuid4()
    catalog.identity = Mock()
    catalog.session_token = "verified-session"
    original = SimpleNamespace(
        organization_id=catalog.organization_id, supplier_id="supplier_a",
        fingerprint="a" * 64, profile_sha256="1" * 64,
        approved_source_sha256="e" * 64, price_basis="standard",
        confirmed_currency="RUB", source_format="csv",
        columns=(("name", 0), ("price", 1)), header_row=1, sheet=None,
        delimiter=";", encoding="utf-8-sig",
    )
    unsupported = SimpleNamespace(
        **{**vars(original), "fingerprint": "b" * 64, "price_basis": "vat_included"},
    )
    return catalog, original, unsupported


def test_original_and_signed_canonical_require_exact_profile_mapping() -> None:
    catalog, original, unsupported = _fixtures()
    original_version = uuid.uuid4()
    canonical_version = uuid.uuid4()
    mapping = SupplierMapping(
        name=0, price=1, header_row=1, delimiter=";", encoding="utf-8-sig",
        confirmed_currency="RUB",
    )
    canonical = SimpleNamespace(
        catalog_version_id=canonical_version,
        original_source_sha256="e" * 64,
        mapping_sha256=mapping_digest(mapping, "reject"),
        canonical_sha256="f" * 64, rows_count=2,
    )
    catalog.connection.execute.side_effect = [
        Mock(all=Mock(return_value=[canonical])),
        Mock(all=Mock(return_value=[
            SimpleNamespace(id=original_version, source_sha256="e" * 64, rows_count=3),
        ])),
    ]
    catalog.connection.scalar.return_value = "installed"
    with patch(
        "kpdesk.catalog.supplier_source_discovery._latest_profiles",
        return_value=({original.fingerprint: original, unsupported.fingerprint: unsupported}, False),
    ):
        result = discover_supplier_sources(catalog, "supplier_a")
    assert not result.truncated and result.canonical_discovery_available
    assert {(item.catalog_version_id, item.evidence) for item in result.sources} == {
        (original_version, "original"), (canonical_version, "reviewed_canonical"),
    }
    assert all(item.profile_fingerprint == original.fingerprint for item in result.sources)
    signature = catalog.connection.execute.call_args_list[0]
    assert signature.args[1]["supplier"] == "supplier_a"
    assert signature.args[1]["org"] == catalog.organization_id


def test_wrong_mapping_and_missing_signed_migration_do_not_promise_source() -> None:
    catalog, original, _ = _fixtures()
    version_id = uuid.uuid4()
    catalog.connection.execute.side_effect = [
        Mock(all=Mock(return_value=[SimpleNamespace(
            catalog_version_id=version_id, original_source_sha256="e" * 64,
            mapping_sha256="0" * 64, canonical_sha256="f" * 64, rows_count=2,
        )])),
        Mock(all=Mock(return_value=[])),
    ]
    catalog.connection.scalar.return_value = "installed"
    with patch(
        "kpdesk.catalog.supplier_source_discovery._latest_profiles",
        return_value=({original.fingerprint: original}, False),
    ):
        assert not discover_supplier_sources(catalog, "supplier_a").sources
    catalog.connection.scalar.return_value = None
    catalog.connection.execute.reset_mock()
    catalog.connection.execute.side_effect = None
    catalog.connection.execute.return_value.all.return_value = [
        SimpleNamespace(id=version_id, source_sha256="f" * 64, rows_count=2),
    ]
    with patch(
        "kpdesk.catalog.supplier_source_discovery._latest_profiles",
        return_value=({original.fingerprint: original}, False),
    ):
        result = discover_supplier_sources(catalog, "supplier_a")
    assert not result.canonical_discovery_available and not result.sources
    assert catalog.connection.execute.call_count == 1


def test_history_head_requires_existing_conflict_aware_sql_latest() -> None:
    catalog, profile, _ = _fixtures()
    # A25/A26 SQL latest() excludes conflicting layout decisions.
    catalog.connection.execute.return_value.all.return_value = []
    with patch(
        "kpdesk.catalog.supplier_source_discovery.TenantSupplierMappingStore.history",
        return_value=[SimpleNamespace(profile=profile)],
    ):
        latest, truncated = _latest_profiles(catalog, "supplier_a")
    assert latest == {} and not truncated
    sql = str(catalog.connection.execute.call_args.args[0])
    assert "kpdesk_supplier_mapping_latest" in sql
    assert catalog.connection.execute.call_args.args[1]["fingerprints"] == [
        profile.fingerprint,
    ]


def test_head_matches_stored_latest_and_reports_truncated_history() -> None:
    catalog, newest, _ = _fixtures()
    older = SimpleNamespace(**{**vars(newest), "approved_source_sha256": "d" * 64})
    catalog.connection.execute.return_value.all.return_value = [
        SimpleNamespace(fingerprint=newest.fingerprint, profile={"validated": True}),
    ]
    calls = 0

    def history(_: object, supplier: str, *, limit: int, offset: int) -> list[object]:
        nonlocal calls
        calls += 1
        assert supplier == "supplier_a"
        return [SimpleNamespace(profile=older)] if offset == 200 else [
            SimpleNamespace(profile=newest),
        ] * 50

    with (
        patch(
            "kpdesk.catalog.supplier_source_discovery.TenantSupplierMappingStore.history",
            autospec=True, side_effect=history,
        ),
        patch(
            "kpdesk.catalog.supplier_source_discovery.parse_profile_payload",
            return_value=newest,
        ),
    ):
        latest, truncated = _latest_profiles(catalog, "supplier_a")
    assert truncated and calls == 5
    assert latest == {newest.fingerprint: newest}


def test_discovery_bounds_combinatorial_profile_version_output() -> None:
    catalog, profile, _ = _fixtures()
    catalog.connection.scalar.return_value = None
    catalog.connection.execute.return_value.all.return_value = [
        SimpleNamespace(
            id=uuid.uuid4(), source_sha256=profile.approved_source_sha256,
            rows_count=1,
        )
        for _ in range(250)
    ]
    with patch(
        "kpdesk.catalog.supplier_source_discovery._latest_profiles",
        return_value=({profile.fingerprint: profile}, False),
    ):
        result = discover_supplier_sources(catalog, "supplier_a")
    assert len(result.sources) == 200 and result.truncated
    assert all(item.evidence == "original" for item in result.sources)
