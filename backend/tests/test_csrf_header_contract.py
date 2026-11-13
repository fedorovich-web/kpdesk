"""Tenant CSRF wire contract remains stable across API and OpenAPI changes."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from kpdesk.main import create_app


@pytest.fixture(scope="module")
def tenant_openapi() -> dict[str, object]:
    # Construct API metadata without connecting to the working PostgreSQL.
    with patch(
        "kpdesk.db.live_service_engines.live_service_engines",
        return_value=(object(), object()),
    ):
        return create_app(testing=True, tenant_sessions=True).openapi()


@pytest.mark.parametrize(("path", "method"), [
    ("/api/session/logout", "post"),
    ("/api/session/organizations/{organization_id}/catalog/import", "post"),
    ("/api/session/organizations/{organization_id}/matches/preview", "post"),
    ("/api/session/organizations/{organization_id}/quotes", "post"),
])
def test_mutating_tenant_routes_declare_csrf_http_header(
    tenant_openapi: dict[str, object], path: str, method: str,
) -> None:
    paths = tenant_openapi["paths"]
    assert isinstance(paths, dict)
    parameters = paths[path][method]["parameters"]
    csrf = [parameter for parameter in parameters
            if parameter["name"].lower() == "x-csrf-token"]
    assert len(csrf) == 1
    assert csrf[0]["in"] == "header"
    # Missing CSRF is handled by session authorization (401), not FastAPI (422).
    assert csrf[0]["required"] is False
    assert not any(parameter["name"] == "csrf" for parameter in parameters)


def test_tenant_path_and_read_only_route_contract(
    tenant_openapi: dict[str, object],
) -> None:
    paths = tenant_openapi["paths"]
    assert isinstance(paths, dict)
    route = paths["/api/session/organizations/{organization_id}/quotes"]
    get_parameters = route["get"]["parameters"]
    assert not any(parameter["name"].lower() == "x-csrf-token"
                   for parameter in get_parameters)
    post_parameters = route["post"]["parameters"]
    assert any(parameter["name"] == "organization_id"
               and parameter["in"] == "path"
               and parameter["required"] for parameter in post_parameters)
