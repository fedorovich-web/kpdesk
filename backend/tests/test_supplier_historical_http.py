"""Historical source audit is separate from quote selection and tenant protected."""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from kpdesk.main import create_app


def test_historical_audit_routes_require_https_session_and_csrf() -> None:
    suffix = "/api/session/organizations/{organization_id}/supplier-price-history"
    assert not any(
        suffix in path for path in create_app(testing=True).openapi()["paths"]
    )
    app = create_app(testing=True, tenant_sessions=True)
    paths = app.openapi()["paths"]
    assert suffix + "/sources" in paths
    assert suffix + "/diff" in paths
    assert set(paths[suffix + "/sources"]) >= {"post"}
    assert set(paths[suffix + "/diff"]) >= {"post"}
    assert "put" not in paths[suffix + "/sources"]
    org = uuid.uuid4()
    root = f"/api/session/organizations/{org}/supplier-price-history"
    body = {
        "before": {
            "supplier_id": "vendor", "profile_sha256": "1" * 64,
            "catalog_version_id": str(uuid.uuid4()),
        },
        "after": {
            "supplier_id": "vendor", "profile_sha256": "2" * 64,
            "catalog_version_id": str(uuid.uuid4()),
        },
    }
    headers = {
        "Origin": "https://localhost:5173",
        "X-CSRF-Token": "unverified",
    }
    with TestClient(app, base_url="https://testserver") as client:
        for path, data in (
            ("/sources", {"supplier_id": "vendor"}),
            ("/diff", body),
        ):
            request = client.post(root + path, json=data, headers=headers)
            assert request.status_code == 401, request.text
            assert request.json()["code"] == "invalid_session"
            no_origin = client.post(root + path, json=data)
            assert no_origin.status_code == 403
            assert no_origin.json()["code"] == "missing_origin"
        missing_digest = client.post(
            root + "/diff",
            headers=headers,
            json={**body, "before": {
                "supplier_id": "vendor",
                "profile_fingerprint": "1" * 64,
                "catalog_version_id": str(uuid.uuid4()),
            }},
        )
        assert missing_digest.status_code == 422
        assert missing_digest.json()["code"] == "validation_error"
        invalid_page = client.post(
            root + "/diff", json={**body, "limit": 101}, headers=headers,
        )
        assert invalid_page.status_code == 422
        assert invalid_page.json()["code"] == "validation_error"
