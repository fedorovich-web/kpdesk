"""Offline HTTP contracts for the read-only service-owner dashboard."""
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from kpdesk.api import service_admin_routes as routes
from kpdesk.main import create_app


@pytest.fixture
def owner_client(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    engine = Mock()
    monkeypatch.setattr('kpdesk.db.live_service_engines.live_service_engines', lambda: (Mock(), engine))
    app = create_app(testing=True, tenant_sessions=True)
    with TestClient(app, base_url='https://testserver') as client:
        yield client, engine


def test_missing_cookie_denies_before_database(owner_client) -> None:  # type: ignore[no-untyped-def]
    http, engine = owner_client
    result = http.get('/api/admin/overview')
    assert result.status_code == 401
    assert result.headers['cache-control'] == 'no-store'
    assert result.headers['x-robots-tag'] == 'noindex, nofollow'
    engine.begin.assert_not_called()


@pytest.mark.parametrize(('state', 'status'), [('invalid_session', 401), ('forbidden', 403)])
def test_gateway_denial_is_preserved(owner_client, monkeypatch, state, status) -> None:  # type: ignore[no-untyped-def]
    http, _ = owner_client
    monkeypatch.setattr(routes, 'read_admin', Mock(return_value={'access': state}))
    http.cookies.set('__Host-kpdesk_session', 'a' * 43)
    assert http.get('/api/admin/overview').status_code == status


def test_users_page_is_bounded_and_response_projects_public_fields(owner_client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    http, _ = owner_client
    read = Mock(return_value={'access': 'allowed', 'data': {'total': 1, 'items': [{
        'id': str(uuid4()), 'email': 'test@example.test', 'status': 'active',
        'created_at': '2026-11-20T13:00:00Z', 'email_verified_at': None,
        'organizations_count': 1, 'password_hash': 'internal',
    }]}})
    monkeypatch.setattr(routes, 'read_admin', read)
    http.cookies.set('__Host-kpdesk_session', 'a' * 43)
    assert http.get('/api/admin/users?page=0').status_code == 422
    assert http.get('/api/admin/users?page=1001').status_code == 422
    assert http.get('/api/admin/users', params={'q': 'a' * 101}).status_code == 422
    read.assert_not_called()
    result = http.get('/api/admin/users', params={'q': 'Test', 'page': 1})
    assert result.status_code == 200
    assert result.json()['total'] == 1
    assert 'password_hash' not in result.json()['items'][0]
    assert read.call_args.args[2:] == ('users', 30, 'test', 1)
    assert http.post('/api/admin/users', headers={'Origin': 'https://testserver'}).status_code in (403, 405)
