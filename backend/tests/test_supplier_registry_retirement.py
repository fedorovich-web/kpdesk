"""Source evidence and price review do not require a business supplier registry."""
from __future__ import annotations

import uuid
from unittest.mock import Mock, patch

from kpdesk.api.tenant_supplier_precision_routes import _authorized_supplier_write
from kpdesk.catalog.supplier_source_discovery import discover_supplier_sources
from kpdesk.main import create_app


def test_tenant_runtime_does_not_expose_retired_registry() -> None:
    with patch(
        "kpdesk.db.live_service_engines.live_service_engines",
        return_value=(Mock(), Mock()),
    ):
        app = create_app(testing=True, tenant_sessions=True)
    assert not any("/suppliers/registry" in path for path in app.openapi()["paths"])


def test_price_review_keeps_authorization_without_registry_lookup() -> None:
    repository, store = Mock(), Mock()
    with patch("kpdesk.api.tenant_supplier_precision_routes._store", return_value=store):
        assert _authorized_supplier_write(
            repository, "session", uuid.uuid4(), "source-group", "csrf",
        ) is store
    store._authorize.assert_called_once_with(write=True)
    store._selector.assert_called_once_with("source-group", "0" * 64)
    repository.connection.scalar.assert_not_called()
    repository.connection.execute.assert_not_called()


def test_source_discovery_does_not_look_up_a_counterparty_status() -> None:
    catalog = Mock()
    catalog.connection.scalar.return_value = None
    with patch(
        "kpdesk.catalog.supplier_source_discovery._latest_profiles", return_value=({}, False),
    ):
        result = discover_supplier_sources(catalog, "source-group")
    assert result.sources == ()
    catalog._authorize.assert_called_once_with("read")
    queries = [str(call.args[0]) for call in catalog.connection.scalar.call_args_list]
    assert len(queries) == 1  # Only the signed-provenance capability lookup remains.
