"""Real HTTP routing/security headers with only the SQL boundary substituted."""
import hashlib
from contextlib import contextmanager
from typing import Any
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from kpdesk.api.tenant_session_routes import get_unreleased_catalog_engine
from kpdesk.db.auth_sessions import AuthSessionRepository, SessionDenied
from kpdesk.main import create_app


@pytest.fixture
def boundary() -> Any:
    conn = Mock()
    conn.scalar.return_value = False

    class OfflineEngine:
        @contextmanager
        def begin(self) -> Any:
            yield conn

    with patch('kpdesk.db.live_service_engines.live_service_engines', return_value=(Mock(), OfflineEngine())):
        app = create_app(testing=True, tenant_sessions=True)
    app.dependency_overrides[get_unreleased_catalog_engine] = lambda: OfflineEngine()
    with patch.object(AuthSessionRepository, 'authenticate', side_effect=SessionDenied()), TestClient(app, base_url='https://testserver') as client:
        client.headers['Origin'] = 'https://localhost:5173'
        yield client, conn


def assert_private(response: Any) -> None:
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['x-robots-tag'] == 'noindex, nofollow'
    assert response.headers['referrer-policy'] == 'no-referrer'
    assert response.headers['x-content-type-options'] == 'nosniff'


def test_public_returns_only_frozen_binary_without_account(boundary: Any) -> None:
    client, conn = boundary
    raw = b'%PDF-approved-fixed'
    conn.execute.return_value.one_or_none.return_value = (raw, hashlib.sha256(raw).hexdigest())
    response = client.post('/api/public/proposal/pdf', headers={'X-Proposal-Token': 'a' * 43})
    assert response.status_code == 200, response.text
    assert response.content == raw
    assert response.headers['content-type'] == 'application/pdf'
    assert_private(response)


@pytest.mark.parametrize('token', [None, 'bad', 'a' * 43])
def test_public_unavailable_has_one_generic_response(boundary: Any, token: str | None) -> None:
    client, conn = boundary
    conn.execute.return_value.one_or_none.return_value = None
    response = client.post('/api/public/proposal/pdf', headers={'X-Proposal-Token': token} if token else {})
    assert response.status_code == 404
    assert response.json()['code'] == 'proposal_unavailable'
    assert_private(response)


def test_invalid_origin_and_missing_origin_are_private_errors(boundary: Any) -> None:
    client, _ = boundary
    response = client.post('/api/public/proposal/pdf', headers={'Origin': 'https://evil.invalid'})
    assert response.status_code == 403
    assert_private(response)
    del client.headers['Origin']
    response = client.post('/api/public/proposal/pdf')
    assert response.status_code == 403
    assert_private(response)


def test_issue_cannot_bypass_session_even_when_schema_is_absent(boundary: Any) -> None:
    client, _ = boundary
    path = f'/api/session/organizations/{uuid4()}/quotes/{uuid4()}/shares'
    response = client.post(path, json={'expected_revision': 2, 'source_snapshot_sha256': 'a' * 64, 'days': 7})
    assert response.status_code == 401
    assert_private(response)
