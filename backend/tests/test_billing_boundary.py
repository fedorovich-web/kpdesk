"""Exact webhook exception and bounded untrusted bodies; no DB or provider I/O."""
from unittest.mock import Mock

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from kpdesk.config import AppSettings
from kpdesk.main import create_app

PATH = '/api/billing/lava/webhook'


def client(monkeypatch: pytest.MonkeyPatch, *, tenant: bool = True) -> TestClient:
    monkeypatch.setattr('kpdesk.db.live_service_engines.live_service_engines',
                        lambda: (Mock(), Mock()))
    app = create_app(testing=True, tenant_sessions=tenant, settings=AppSettings(
        allowed_tenant_origins=('https://kpdesk.ru',)))
    # FastAPI may retain included routers lazily. Test the middleware with only
    # this sentinel; actual webhook authentication is tested in test_billing_http.
    app.router.routes.clear()

    @app.post(PATH)
    async def sentinel(body: dict[str, object], request: Request) -> dict[str, bool]:
        return {'bounded': True}

    return TestClient(app, base_url='https://testserver')


def test_only_exact_post_webhook_bypasses_browser_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    with client(monkeypatch) as http:
        assert http.post(PATH, json={}).status_code == 200
        for path in [PATH+'/', PATH+'/extra', '/api/session/login', '/api/billing/lava/other']:
            assert http.post(path, json={}).status_code == 403
        assert http.post(PATH, json={}, headers={'Origin': 'https://evil.invalid'}).status_code == 403
        assert http.post(PATH, json={}, headers={'Host': 'evil.invalid'}).status_code == 400
    with client(monkeypatch, tenant=False) as http:
        assert http.post(PATH, json={}).status_code == 403


def test_exact_webhook_still_requires_https(monkeypatch: pytest.MonkeyPatch) -> None:
    with client(monkeypatch) as http:
        assert http.post('http://testserver'+PATH, json={}).status_code == 403


def test_webhook_declared_and_chunked_body_capped_before_route(monkeypatch: pytest.MonkeyPatch) -> None:
    oversized = b'{"data":"'+b'x'*65536+b'"}'
    with client(monkeypatch) as http:
        assert http.post(PATH, content=oversized,
                         headers={'Content-Type': 'application/json'}).status_code == 413
        assert http.post(PATH, content=iter([oversized[:200], oversized[200:]]),
                         headers={'Content-Type': 'application/json'}).status_code == 413
        assert http.post(PATH, json={}, headers={'Content-Length': '-1'}).status_code == 400


def test_billing_sql_error_whitelist_never_leaks_other_database_diagnostics() -> None:
    from types import SimpleNamespace

    from sqlalchemy.exc import DBAPIError

    from kpdesk.billing.errors import billing_error

    raw = Exception('private SQL with credentials')
    raw.diag = SimpleNamespace(message_primary='kpdesk_billing_quote_limit')  # type: ignore[attr-defined]
    wrapped = DBAPIError('private query', {}, raw)
    api = RuntimeError('wrapped'); api.__cause__ = wrapped
    result = billing_error(api)
    assert result and result.status_code == 409 and result.code == 'billing_quote_limit'
    raw.diag.message_primary = 'secret from unrelated database failure'  # type: ignore[attr-defined]
    assert billing_error(api) is None
