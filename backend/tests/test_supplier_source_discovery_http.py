"""Source discovery API never trusts supplier labels as price evidence."""
from __future__ import annotations

import uuid
from unittest.mock import Mock, patch

import pytest
from fastapi import Response

from kpdesk.api.errors import ApiError
from kpdesk.api.tenant_supplier_comparison_routes import (
    SupplierDiscoverBody,
    discover_supplier_quote_sources,
)
from kpdesk.catalog.supplier_source_discovery import (
    DiscoveredSupplierSource,
    SupplierSourceDiscovery,
)


def test_discovery_response_contains_only_server_verified_identifiers() -> None:
    org = uuid.uuid4()
    version = uuid.uuid4()
    response = Response()
    repository = Mock()
    entry = SupplierSourceDiscovery(
        "supplier_a",
        (DiscoveredSupplierSource(
            "supplier_a", "a" * 64, version, "f" * 64, 2,
            "reviewed_canonical",
        ),),
        truncated=False, canonical_discovery_available=True,
    )
    with (
        patch(
            "kpdesk.api.tenant_supplier_comparison_routes._store",
            return_value=Mock(identity=Mock()),
        ) as authorized,
        patch(
            "kpdesk.api.tenant_supplier_comparison_routes.discover_supplier_sources",
            side_effect=[
                entry,
                SupplierSourceDiscovery("supplier_b", (), False, True),
            ],
        ) as discover,
    ):
        result = discover_supplier_quote_sources(
            org, SupplierDiscoverBody(supplier_ids=("supplier_a", "supplier_b")),
            response, repository, session_cookie="session", csrf="csrf",
        )
    authorized.assert_called_once_with(
        repository, "session", org, write=True, csrf="csrf",
    )
    assert [call.args[1] for call in discover.call_args_list] == [
        "supplier_a", "supplier_b",
    ]
    assert response.headers["Cache-Control"] == "no-store"
    assert result.suppliers[0].options[0].catalog_version_id == version
    assert result.suppliers[0].options[0].evidence == "reviewed_canonical"
    assert "price_rub" not in result.model_dump_json()


def test_duplicate_supplier_names_refused_before_repository_access() -> None:
    with (
        patch("kpdesk.api.tenant_supplier_comparison_routes._store") as authorize,
        pytest.raises(ApiError),
    ):
        discover_supplier_quote_sources(
            uuid.uuid4(),
            SupplierDiscoverBody(supplier_ids=("supplier_a", "supplier_a")),
            Response(), Mock(), session_cookie="session", csrf="csrf",
        )
    authorize.assert_not_called()
