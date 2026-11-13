"""New comparison/compose routes remain tenant session+CSRF protected."""
from __future__ import annotations

import json
import uuid

from fastapi.testclient import TestClient

from kpdesk.api.tenant_supplier_comparison_routes import SupplierCompareBody
from kpdesk.main import create_app


def _body() -> dict[str, object]:
    return {
        "sources": [
            {
                "supplier_id": "supplier_a",
                "profile_fingerprint": "a" * 64,
                "catalog_version_id": str(uuid.uuid4()),
            },
            {
                "supplier_id": "supplier_b",
                "profile_fingerprint": "b" * 64,
                "catalog_version_id": str(uuid.uuid4()),
            },
        ],
        "extracted_request": {"lines": [{
            "source_quote": "Карандаш графитовый HB",
            "description": "Карандаш графитовый HB", "category": "pencil",
            "quantity": 2, "unit": "штука", "brand": None,
            "paper_format": None, "paper_gsm": None,
            "pen_ink_color": None, "pen_ink_type": None,
            "attributes": {"pencil_type": "graphite", "lead_grade": "HB"},
        }]},
    }


def test_comparison_and_composition_require_session_and_are_not_in_legacy() -> None:
    SupplierCompareBody.model_validate_json(json.dumps(_body()))
    org = uuid.uuid4()
    route = f"/api/session/organizations/{org}/quotes/supplier-comparison"
    local = create_app(testing=True)
    paths = local.openapi()["paths"]
    assert not any("supplier-comparison" in path for path in paths)
    app = create_app(testing=True, tenant_sessions=True)
    published = app.openapi()["paths"]
    assert route.replace(str(org), "{organization_id}") in published
    assert route.replace(str(org), "{organization_id}") + "/preview" in published
    assert route.replace(str(org), "{organization_id}") + "/sources" in published
    assert route.replace(str(org), "{organization_id}") + "/supplier-sources" in published
    assert route.replace(str(org), "{organization_id}") + "/source-diff" in published
    assert route.replace(str(org), "{organization_id}") + "/source-diff" in published
    with TestClient(app, base_url="https://testserver") as client:
        headers = {
            "Origin": "https://localhost:5173",
            "X-CSRF-Token": "non-session",
            "Idempotency-Key": "supplier-quote-no-login",
        }
        discovery = client.post(
            route + "/sources",
            json={"supplier_ids": ["supplier_a", "supplier_b"]}, headers=headers,
        )
        single = client.post(
            route + "/supplier-sources",
            json={"supplier_id": "supplier_a"}, headers=headers,
        )
        assert single.status_code == 401 and single.json()["code"] == "invalid_session"
        assert discovery.status_code == 401
        assert discovery.json()["code"] == "invalid_session"
        source_diff_body = {
            "before": {
                "supplier_id": "supplier_a",
                "profile_fingerprint": "a" * 64,
                "catalog_version_id": str(uuid.uuid4()),
            },
            "after": {
                "supplier_id": "supplier_a",
                "profile_fingerprint": "b" * 64,
                "catalog_version_id": str(uuid.uuid4()),
            },
            "offset": 0, "limit": 50,
        }
        diff = client.post(route + "/source-diff", json=source_diff_body, headers=headers)
        assert diff.status_code == 401
        assert diff.json()["code"] == "invalid_session"
        bad = client.post(
            route + "/source-diff",
            json={**source_diff_body, "limit": 1000},
            headers=headers,
        )
        assert bad.status_code == 422
        result = client.post(route + "/preview", json=_body(), headers=headers)
        assert result.status_code == 401, result.text
        assert result.json()["code"] == "invalid_session"
        result = client.post(route, json={
            **_body(),
            "active_catalog_version_id": str(uuid.uuid4()),
            "selections": [],
        }, headers=headers)
        assert result.status_code == 401, result.text
        assert result.json()["code"] == "invalid_session"
        result = client.post(route + "/preview", json=_body())
        assert result.status_code == 403
        assert result.json()["code"] == "missing_origin"
