"""A102 historical mapping evidence is not approval for a new quote."""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from kpdesk.catalog.supplier_historical_sources import (
    HistoricalSupplierRef,
    _load_historical_source,
    compare_historical_supplier_sources,
    discover_historical_supplier_sources,
)
from kpdesk.quotes.supplier_comparison import VerifiedSupplierCatalog


def _catalog() -> Mock:
    catalog = Mock()
    catalog.organization_id = uuid.uuid4()
    catalog.session_token = "authenticated"
    return catalog


def _profile(catalog: Mock, digest: str, proof: str) -> SimpleNamespace:
    return SimpleNamespace(
        organization_id=catalog.organization_id, supplier_id="supplier",
        fingerprint="f" * 64, profile_sha256=proof,
        approved_source_sha256=digest, price_basis="standard",
        confirmed_currency="RUB",
    )


def test_historical_discovery_binds_each_review_digest_to_original_bytes() -> None:
    catalog = _catalog()
    a = _profile(catalog, "a" * 64, "1" * 64)
    b = _profile(catalog, "b" * 64, "2" * 64)
    old, new = uuid.uuid4(), uuid.uuid4()
    catalog.connection.execute.return_value.all.return_value = [
        SimpleNamespace(id=old, source_sha256="a" * 64, rows_count=5),
        SimpleNamespace(id=new, source_sha256="b" * 64, rows_count=5),
    ]
    with patch(
        "kpdesk.catalog.supplier_historical_sources.TenantSupplierMappingStore.history",
        return_value=[SimpleNamespace(profile=a), SimpleNamespace(profile=b)],
    ) as history:
        result = discover_historical_supplier_sources(catalog, "supplier")
    assert history.call_count == 1
    assert result.audit_only is True and not result.truncated
    assert {(x.profile_sha256, x.catalog_version_id, x.source_sha256)
            for x in result.options} == {
                ("1" * 64, old, "a" * 64), ("2" * 64, new, "b" * 64),
            }
    params = catalog.connection.execute.call_args.args[1]
    assert params["org"] == catalog.organization_id
    assert set(params["digests"]) == {"a" * 64, "b" * 64}


def test_historical_loader_only_accepts_exact_append_only_profile() -> None:
    catalog = _catalog()
    before = HistoricalSupplierRef("supplier", "1" * 64, uuid.uuid4())
    profile = _profile(catalog, "a" * 64, "1" * 64)
    with (
        patch("kpdesk.catalog.supplier_historical_sources._history",
              return_value=((profile,), False)),
        patch("kpdesk.catalog.supplier_historical_sources._verified_profile_catalog",
              return_value=Mock(spec=VerifiedSupplierCatalog)) as verify,
    ):
        _load_historical_source(catalog, before)
    assert verify.call_count == 1
    assert verify.call_args.args[2] is profile
    assert verify.call_args.args[1].profile_fingerprint == profile.fingerprint
    assert verify.call_args.args[1].catalog_version_id == before.catalog_version_id


@pytest.mark.parametrize("selector", [
    HistoricalSupplierRef("supplier", "invalid", uuid.uuid4()),
    HistoricalSupplierRef("wrong tenant", "1" * 64, uuid.uuid4()),
])
def test_invalid_historical_ref_rejected_before_db(selector: HistoricalSupplierRef) -> None:
    catalog = _catalog()
    with pytest.raises(ValueError), patch(
        "kpdesk.catalog.supplier_historical_sources._history"
    ) as history:
        _load_historical_source(catalog, selector)
    history.assert_not_called()


def test_historical_profile_not_found_and_history_truncation_fail_closed() -> None:
    catalog = _catalog()
    ref = HistoricalSupplierRef("supplier", "1" * 64, uuid.uuid4())
    with patch(
        "kpdesk.catalog.supplier_historical_sources._history",
        return_value=((), False),
    ), pytest.raises(ValueError, match="historical_review_not_found"):
        _load_historical_source(catalog, ref)
    with patch(
        "kpdesk.catalog.supplier_historical_sources._history",
        return_value=((), True),
    ), pytest.raises(ValueError, match="beyond_bounded_history"):
        _load_historical_source(catalog, ref)


def test_cross_supplier_and_same_version_disallowed_before_history_read() -> None:
    catalog = _catalog()
    version = uuid.uuid4()
    old = HistoricalSupplierRef("supplier", "1" * 64, version)
    with patch("kpdesk.catalog.supplier_historical_sources._history") as history:
        with pytest.raises(ValueError, match="cross_supplier"):
            compare_historical_supplier_sources(
                catalog, old, HistoricalSupplierRef("outsider", "2" * 64, uuid.uuid4()),
            )
        with pytest.raises(ValueError, match="same_version"):
            compare_historical_supplier_sources(
                catalog, old, HistoricalSupplierRef("supplier", "2" * 64, version),
            )
    history.assert_not_called()


def test_unverified_old_catalog_digest_rejected_at_provenance_gate() -> None:
    catalog = _catalog()
    old, new = uuid.uuid4(), uuid.uuid4()
    with patch(
        "kpdesk.catalog.supplier_historical_sources._load_historical_source",
        side_effect=[Mock(spec=VerifiedSupplierCatalog),
                     ValueError("supplier_catalog_digest_not_reviewed")],
    ), pytest.raises(ValueError, match="digest_not_reviewed"):
        compare_historical_supplier_sources(
            catalog, HistoricalSupplierRef("supplier", "1" * 64, old),
            HistoricalSupplierRef("supplier", "2" * 64, new),
        )
