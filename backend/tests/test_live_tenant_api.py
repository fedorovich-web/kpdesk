"""Real PostgreSQL session app contract; no fabricated owner sessions."""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import text

from kpdesk.db.live_service_engines import live_service_engines
from kpdesk.main import create_app


def test_live_service_roles_have_independent_connections_and_grants() -> None:
    auth, tenant = live_service_engines()
    with auth.begin() as a, tenant.begin() as t:
        assert a.scalar(text("SELECT current_user")) == "kpdesk_auth_runtime"
        assert t.scalar(text("SELECT current_user")) == "kpdesk_tenant_runtime"
        assert a.scalar(text("SELECT pg_backend_pid()")) != t.scalar(
            text("SELECT pg_backend_pid()")
        )
        assert a.scalar(text("""
            SELECT has_table_privilege(current_user,'public.catalog_items','SELECT')
        """)) is False
        assert t.scalar(text("""
            SELECT has_function_privilege(
                current_user,'public.kpdesk_auth_credential(text,text)','EXECUTE')
        """)) is False


def test_explicit_https_tenant_app_excludes_old_owner_routes() -> None:
    tenant_app = create_app(testing=True, tenant_sessions=True)
    organization = uuid.uuid4()
    with TestClient(
        tenant_app, base_url="https://testserver",
        client=("127.0.0.1", 52025),
    ) as client:
        assert client.get("/api/health/live").status_code == 200
        assert client.get("/api/health/ready").status_code == 200
        assert client.get("/api/catalog/current").status_code == 404
        assert client.get("/api/quotes/drafts").status_code == 404
        assert client.get("/api/session/me").status_code == 401
        assert client.get(
            f"/api/session/organizations/{organization}/catalog/current"
        ).status_code == 401
        headers = {"Origin": "https://localhost:5173", "X-CSRF-Token": "invalid"}
        assert client.post(
            f"/api/session/organizations/{organization}/matches/category-suggestion",
            json={"description": "Бумага А4", "original_category": "paper"},
            headers=headers,
        ).status_code == 401
        assert client.post(
            f"/api/session/organizations/{organization}/matches/preview",
            json={
                "catalog_version_id": str(uuid.uuid4()),
                "requested_item": {
                    "description": "Бумага А4", "category": "paper",
                },
            },
            headers=headers,
        ).status_code == 401
        assert client.post(
            f"/api/session/organizations/{organization}/matches/preview",
            json={
                "catalog_version_id": str(uuid.uuid4()),
                "mode": "rapidfuzz",
                "requested_item": {"description": "Бумага А4", "category": "paper"},
            },
            headers=headers,
        ).status_code == 401
        headers = {
            "Origin": "https://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "X-CSRF-Token",
        }
        preflight = client.options("/api/session/login", headers=headers)
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-credentials"] == "true"


def test_session_app_requires_https_and_default_app_has_no_sessions() -> None:
    normal = create_app(testing=True)
    with TestClient(normal) as client:
        assert client.get("/api/session/me").status_code == 404
    app = create_app(testing=True, tenant_sessions=True)
    with TestClient(app, base_url="http://testserver") as client:
        denied = client.get("/api/session/me")
        assert denied.status_code == 403
        assert denied.json()["code"] == "https_required"


def test_catalog_preflight_requires_session_even_for_invalid_file() -> None:
    app = create_app(testing=True, tenant_sessions=True)
    organization = uuid.uuid4()
    with TestClient(app, base_url="https://testserver", client=("127.0.0.1", 52025)) as client:
        response = client.post(
            f"/api/session/organizations/{organization}/catalog/preflight",
            headers={"Origin": "https://localhost:5173", "X-CSRF-Token": "invalid"},
            files={"file": ("bad.csv", b"not a catalog", "text/csv")},
        )
        assert response.status_code == 401
        assert response.json()["code"] == "invalid_session"
        assert client.post(
            f"/api/session/organizations/{organization}/catalog/import",
            headers={"Origin": "https://localhost:5173",
                     "X-CSRF-Token": "invalid", "Idempotency-Key": "one"},
            files={"file": ("bad.csv", b"not a catalog", "text/csv")},
        ).status_code == 401
