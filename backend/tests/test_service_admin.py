"""Offline HTTP contracts for the service-owner dashboard."""
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from kpdesk.api import service_admin_routes as routes
from kpdesk.config import AppSettings
from kpdesk.db.auth_sessions import _token_digest
from kpdesk.main import create_app


@pytest.fixture
def owner_client(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    engine = Mock()
    monkeypatch.setattr('kpdesk.db.live_service_engines.live_service_engines', lambda: (Mock(), engine))
    app = create_app(testing=True, tenant_sessions=True, settings=AppSettings(
        allowed_tenant_origins=('https://testserver',),
    ))
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


@pytest.mark.parametrize(('headers', 'cookie', 'status'), [
    ({}, True, 403),
    ({'Origin': 'https://cross-origin.test', 'X-CSRF-Token': 'b' * 43}, True, 403),
    ({'Origin': 'https://testserver'}, True, 403),
    ({'Origin': 'https://testserver', 'X-CSRF-Token': 'invalid'}, True, 403),
    ({'Origin': 'https://testserver', 'X-CSRF-Token': 'b' * 43}, False, 401),
])
def test_moderation_requires_origin_session_and_csrf(owner_client, headers, cookie, status) -> None:  # type: ignore[no-untyped-def]
    http, engine = owner_client
    if cookie:
        http.cookies.set('__Host-kpdesk_session', 'a' * 43)
    result = http.post(f'/api/admin/users/{uuid4()}/actions', headers=headers, json={'action': 'block'})
    assert result.status_code == status
    assert result.headers['cache-control'] == 'no-store'
    engine.begin.assert_not_called()


@pytest.mark.parametrize(('access', 'status'), [
    ('allowed', 200), ('invalid_session', 401), ('forbidden', 403), ('invalid_csrf', 403),
    ('protected', 403), ('missing', 404), ('conflict', 409),
])
def test_moderation_preserves_gateway_decisions(owner_client, monkeypatch, access, status) -> None:  # type: ignore[no-untyped-def]
    http, engine = owner_client
    user_id = uuid4()
    mutate = Mock(return_value={'access': access, 'data': {
        'id': str(user_id), 'status': 'disabled', 'is_blocked': True, 'sessions_revoked': 2,
    }})
    monkeypatch.setattr(routes, 'moderate_user', mutate, raising=False)
    http.cookies.set('__Host-kpdesk_session', 'a' * 43)
    result = http.post(f'/api/admin/users/{user_id}/actions',
                       headers={'Origin': 'https://testserver', 'X-CSRF-Token': 'b' * 43},
                       json={'action': 'block'})
    assert result.status_code == status
    mutate.assert_called_once_with(engine, _token_digest('a' * 43), _token_digest('b' * 43), user_id, 'block')
    if access == 'allowed':
        assert result.json()['sessions_revoked'] == 2


def test_moderation_rejects_arbitrary_fields_and_actions(owner_client) -> None:  # type: ignore[no-untyped-def]
    http, engine = owner_client
    http.cookies.set('__Host-kpdesk_session', 'a' * 43)
    headers = {'Origin': 'https://testserver', 'X-CSRF-Token': 'b' * 43}
    path = f'/api/admin/users/{uuid4()}/actions'
    assert http.post(path, headers=headers, json={'action': 'delete'}).status_code == 422
    assert http.post(path, headers=headers, json={'action': 'block', 'status': 'active'}).status_code == 422
    engine.begin.assert_not_called()
