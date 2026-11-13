"""Exercise privacy through the actual ASGI boundary, with no physical services."""
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Engine

from kpdesk.api import universal_routes as routes
from kpdesk.api.errors import ApiError
from kpdesk.api.tenant_session_routes import get_unreleased_catalog_engine
from kpdesk.main import create_app


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['success', 'withdraw', 'invalid_session', 'forbidden',
                                  'corrupt_snapshot', 'validation', 'missing_origin', 'http'])
async def test_universal_privacy_applies_to_success_and_refusals(
    monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    app = create_app(testing=True)
    app.include_router(routes.router)
    app.dependency_overrides[get_unreleased_catalog_engine] = lambda: Mock(spec=Engine)
    statuses = {'invalid_session': 401, 'forbidden': 403, 'corrupt_snapshot': 503}

    @contextmanager
    def fake_store(*args: object, **kwargs: object) -> Iterator[Mock]:
        if case in statuses:
            raise ApiError(statuses[case], case, 'Synthetic refusal')
        store = Mock()
        store.export_feedback.return_value = []
        yield store

    monkeypatch.setattr(routes, '_store', fake_store)
    path = f'/api/session/organizations/{uuid4()}/universal/'
    method, endpoint, expected = 'GET', 'feedback/export', statuses.get(case, 200)
    headers = {'Origin': app.state.settings.allowed_origins[0]}
    if case == 'withdraw':
        method, endpoint, expected = 'DELETE', f'feedback/{uuid4()}', 204
    elif case == 'validation':
        endpoint, expected = 'feedback/export?offset=-1', 422
    elif case == 'missing_origin':
        method, endpoint, expected, headers = 'DELETE', f'feedback/{uuid4()}', 403, {}
    elif case == 'http':
        method, expected = 'PATCH', 405
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url='http://testserver') as client:
        response = await client.request(method, path + endpoint, headers=headers)
        public = await client.get('/api/not-a-route')
    assert response.status_code == expected
    assert response.headers.get('cache-control') == 'no-store'
    assert public.headers.get('cache-control') is None
