"""Public-domain ASGI contract: fail closed, no legacy global API."""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kpdesk.production import create_production_app, public_tenant_settings


@pytest.mark.parametrize("origin", [
    "", "http://kpdesk.ru", "https://localhost", "https://127.0.0.1",
    "https://app.localhost", "https://tenant.test", "https://service.internal",
    "https://kpdesk.ru:443", "https://kpdesk.ru/path", "https://kpdesk.ru/",
    "https://kpdesk.ru?x=1", "https://kpdesk.ru#token",
    "https://admin:pass@kpdesk.ru", "https://kpdesk.ru.evil.test:443",
    "https://kpdesk.ru,https://evil.test",
])
def test_public_origin_rejects_unsafe_or_ambiguous_values(origin: str) -> None:
    with pytest.raises(ValueError):
        public_tenant_settings({"KP_DESK_PUBLIC_ORIGIN": origin})


def test_public_origin_controls_exact_trusted_host_and_excludes_synthetic_ml() -> None:
    settings = public_tenant_settings({"KP_DESK_PUBLIC_ORIGIN": "https://kpdesk.ru"})
    assert settings.allowed_hosts == ("kpdesk.ru",)
    assert settings.allowed_tenant_origins == ("https://kpdesk.ru",)
    assert settings.allowed_origins == ()
    assert not settings.enable_synthetic_ml


def test_production_fails_when_database_mount_is_not_explicit() -> None:
    with (
        patch.dict(os.environ, {"KP_DESK_PUBLIC_ORIGIN": "https://kpdesk.ru"}, clear=True),
        pytest.raises(RuntimeError, match="KP_DESK_DB_HOST"),
    ):
        create_production_app()


def _make_production_api() -> FastAPI:
    variables = {
        "KP_DESK_PUBLIC_ORIGIN": "https://kpdesk.ru",
        "KP_DESK_DB_HOST": "db",
        "KP_DESK_DB_PORT": "5432",
        "KP_DESK_SERVICE_CREDENTIALS_FILE": "/run/secrets/kpdesk-runtime-db.json",
    }
    auth, tenant = MagicMock(), MagicMock()
    tenant.begin.return_value.__enter__.return_value.execute.return_value.one.return_value = (
        'kpdesk_tenant_runtime', '0042_openrouter_budget', True, True, True,
    )
    tenant.begin.return_value.__enter__.return_value.execute.return_value.scalar_one.return_value = True
    with patch.dict(os.environ, variables), patch(
        "kpdesk.db.live_service_engines.live_service_engines",
        return_value=(auth, tenant),
    ):
        return create_production_app()


def test_public_asgi_factory_excludes_legacy_api_and_untrusted_origins() -> None:
    app = _make_production_api()
    with TestClient(app, base_url="https://kpdesk.ru") as client:
        assert client.get("/api/health/live").status_code == 200
        assert client.get("/api/catalog/current").status_code == 404
        assert client.get("/api/quotes/drafts").status_code == 404
        assert "/api/session/me" in app.openapi()["paths"]
        allowed = client.options("/api/session/login", headers={
            "Origin": "https://kpdesk.ru",
            "Access-Control-Request-Method": "POST",
        })
        assert allowed.status_code == 200
        assert allowed.headers["access-control-allow-origin"] == "https://kpdesk.ru"
        assert allowed.headers["access-control-allow-credentials"] == "true"
        refused = client.post(
            "/api/session/login",
            headers={"Origin": "https://untrusted.example"},
            json={"email": "test@example.invalid", "password": "placeholder"},
        )
        assert refused.status_code == 403
        assert refused.json()["code"] == "invalid_origin"
        no_origin = client.post(
            "/api/session/login",
            json={"email": "test@example.invalid", "password": "placeholder"},
        )
        assert no_origin.status_code == 403
        assert no_origin.json()["code"] == "missing_origin"


def test_public_asgi_rejects_invalid_host_and_insecure_transport() -> None:
    app = _make_production_api()
    with TestClient(app, base_url="https://untrusted.example") as client:
        denied = client.get("/api/health/live")
        assert denied.status_code == 400
        assert denied.json()["code"] == "invalid_host"
    with TestClient(app, base_url="http://kpdesk.ru") as client:
        denied = client.get("/api/health/live")
        assert denied.status_code == 403
        assert denied.json()["code"] == "https_required"
