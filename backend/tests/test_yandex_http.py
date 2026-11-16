"""Real ASGI/cookie boundary, injected OAuth orchestration and zero provider I/O."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from kpdesk.api import yandex_routes as routes
from kpdesk.config import AppSettings
from kpdesk.db.auth_sessions import IssuedSession
from kpdesk.main import create_app
from kpdesk.yandex.client import YandexError
from kpdesk.yandex.service import ClaimedAttempt, OAuthOutcome, OAuthStart

STATE, BROWSER = 's' * 43, 'b' * 43


def setup(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr('kpdesk.db.live_service_engines.live_service_engines', lambda: (Mock(), Mock()))
    app = create_app(testing=True, tenant_sessions=True, settings=AppSettings(
        allowed_tenant_origins=('https://testserver',)))
    attempt = ClaimedAttempt('a' * 64, 'c' * 64, 'v' * 64, 'login', '/app')
    issued = IssuedSession('t' * 43, 'c' * 43, uuid4(), datetime.now(UTC) + timedelta(days=7))
    service = SimpleNamespace(provider_available=Mock(return_value=True),
        start=Mock(return_value=OAuthStart('https://oauth.yandex.ru/authorize?state=' + STATE, BROWSER)),
        claim=Mock(return_value=attempt), finish=AsyncMock(return_value=OAuthOutcome(issued, '/app')),
        identity=Mock(return_value=True))
    app.dependency_overrides[routes.get_yandex_service] = lambda: service
    return TestClient(app, base_url='https://testserver', follow_redirects=False), service


def test_capability_and_start_set_only_short_browser_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    http, service = setup(monkeypatch)
    with http:
        assert http.get('/api/session/providers').json() == {'yandex': True}
        response = http.get('/api/session/yandex/start?next=/admin')
    assert response.status_code == 303
    assert response.headers['location'].startswith('https://oauth.yandex.ru/authorize?')
    cookie = response.headers['set-cookie']
    assert '__Host-kpdesk_yandex_flow=' in cookie and 'Max-Age=600' in cookie
    assert all(flag in cookie for flag in ('HttpOnly', 'Secure', 'Path=/', 'SameSite=lax'))
    assert '__Host-kpdesk_session=' not in cookie
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['referrer-policy'] == 'no-referrer'
    service.start.assert_called_once()


def test_callback_sets_lax_session_after_service_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    http, service = setup(monkeypatch)
    with http:
        http.cookies.set('__Host-kpdesk_yandex_flow', BROWSER)
        response = http.get('/api/session/yandex/callback', params={'state': STATE, 'code': 'private-code'})
    assert response.status_code == 303 and response.headers['location'] == '/app'
    cookies = response.headers.get_list('set-cookie')
    primary = next(value for value in cookies if value.startswith('__Host-kpdesk_session='))
    assert all(flag in primary for flag in ('HttpOnly', 'Secure', 'Path=/', 'SameSite=lax'))
    assert any(value.startswith('__Host-kpdesk_yandex_flow=') and 'Max-Age=0' in value for value in cookies)
    assert 'private-code' not in str(response.headers) and STATE not in response.text
    service.claim.assert_called_once_with(STATE, BROWSER)
    service.finish.assert_awaited_once()


@pytest.mark.parametrize('query', ['', '?state=' + STATE, '?code=private-code',
    '?state=' + STATE + '&state=' + STATE + '&code=private-code',
    '?state=' + STATE + '&code=one&code=two'])
def test_incomplete_or_ambiguous_callback_never_claims(monkeypatch: pytest.MonkeyPatch, query: Any) -> None:
    http, service = setup(monkeypatch)
    with http:
        http.cookies.set('__Host-kpdesk_yandex_flow', BROWSER)
        response = http.get('/api/session/yandex/callback' + query)
    assert response.status_code == 303
    assert response.headers['location'] == '/auth/login?oauth_error=yandex_flow_expired'
    assert '__Host-kpdesk_session=' not in str(response.headers)
    service.claim.assert_not_called()
    service.finish.assert_not_awaited()


def test_wrong_browser_and_provider_denial_have_safe_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    http, service = setup(monkeypatch)
    service.claim.side_effect = YandexError('yandex_flow_expired')
    with http:
        http.cookies.set('__Host-kpdesk_yandex_flow', 'x' * 43)
        response = http.get('/api/session/yandex/callback', params={'state': STATE, 'code': 'private-code'})
    assert response.status_code == 303 and 'yandex_flow_expired' in response.headers['location']
    service.finish.assert_not_awaited()


@pytest.mark.parametrize('failure', [YandexError('yandex_account_link_required'),
                                     SQLAlchemyError('private-provider-secret')])
def test_failed_completion_never_sets_session_cookie_or_leaks_details(monkeypatch: pytest.MonkeyPatch, failure: Any) -> None:
    http, service = setup(monkeypatch)
    service.finish.side_effect = failure
    with http:
        http.cookies.set('__Host-kpdesk_yandex_flow', BROWSER)
        response = http.get('/api/session/yandex/callback', params={'state': STATE, 'code': 'private-code'})
    assert response.status_code == 303 and '__Host-kpdesk_session=' not in str(response.headers)
    assert 'private-provider-secret' not in str(response.headers) + response.text
    assert response.headers['x-robots-tag'] == 'noindex, nofollow'


def test_link_requires_origin_cookie_and_csrf(monkeypatch: pytest.MonkeyPatch) -> None:
    http, service = setup(monkeypatch)
    with http:
        path = '/api/session/yandex/link/start'
        assert http.post(path, json={}).status_code == 403
        assert http.post(path, json={}, headers={'Origin': 'https://evil.test'}).status_code == 403
        assert http.post(path, json={}, headers={'Origin': 'https://testserver'}).status_code == 422
        response = http.post(path, json={}, headers={'Origin': 'https://testserver', 'X-CSRF-Token': 'c' * 43})
        assert response.status_code == 401
        http.cookies.set('__Host-kpdesk_session', 't' * 43)
        response = http.post(path, json={'next_path': '/app'},
            headers={'Origin': 'https://testserver', 'X-CSRF-Token': 'c' * 43})
    assert response.status_code == 200 and response.json()['authorization_url'].startswith('https://oauth.yandex.ru/')
    service.start.assert_called_once()
    assert service.start.call_args.args[0] == 'link'


def test_identity_requires_cookie_and_exposes_only_boolean(monkeypatch: pytest.MonkeyPatch) -> None:
    http, service = setup(monkeypatch)
    with http:
        assert http.get('/api/session/yandex/identity').status_code == 401
        http.cookies.set('__Host-kpdesk_session', 't' * 43)
        response = http.get('/api/session/yandex/identity')
    assert response.json() == {'linked': True}
    service.identity.assert_called_once_with('t' * 43)


def test_default_local_application_has_no_oauth_routes() -> None:
    with TestClient(create_app(testing=True), base_url='https://testserver') as http:
        assert http.get('/api/session/yandex/start').status_code == 404
