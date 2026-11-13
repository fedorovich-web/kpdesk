"""Owner/tenant/CSRF and immutable snapshot boundaries, offline HTTP transport."""
from collections.abc import Iterator
from typing import TypedDict, cast
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import Connection
from test_document_alternatives import saved

from kpdesk.api import tenant_quote_routes as routes
from kpdesk.api.errors import ApiError, json_error
from kpdesk.api.tenant_session_routes import SESSION_COOKIE, get_unreleased_auth_repository
from kpdesk.catalog.tenant_storage import TenantCatalogRepository
from kpdesk.db.auth_sessions import AuthSessionRepository, SessionDenied
from kpdesk.db.tenant_access import AuthenticatedUser, TenantAccessDenied
from kpdesk.matching.schema import CatalogProduct
from kpdesk.quotes.storage import StoredQuote
from kpdesk.quotes.tenant_storage import TenantQuoteRepository

ORG = UUID('11111111-1111-1111-1111-111111111111')
ACTOR = UUID('22222222-2222-4222-8222-222222222222')


class State(TypedDict):
    owner: bool
    revoked: bool
    late_revoke: bool
    quote: StoredQuote


type Boundary = tuple[TestClient, State, Mock]


@pytest.fixture
def boundary(monkeypatch: pytest.MonkeyPatch) -> Iterator[Boundary]:
    quote, products = saved()
    state: State = {'owner': True, 'revoked': False, 'late_revoke': False, 'quote': quote}

    def authenticate(self: object, token: str, *, operation: str, csrf: str | None) -> AuthenticatedUser:
        if token != 'session' or csrf != 'csrf' or state['revoked']:
            raise SessionDenied()
        return AuthenticatedUser(ACTOR)

    def authorize(connection: object, *, session_token: str, organization_id: UUID,
                  operation: str, csrf: str | None) -> None:
        assert operation == 'manage'
        if organization_id != ORG or not state['owner']:
            raise TenantAccessDenied()

    def items(self: object, version: UUID) -> list[CatalogProduct]:
        assert version == quote.catalog_version_id
        if state['late_revoke']:
            state['owner'] = False
        return products

    monkeypatch.setattr(AuthSessionRepository, 'authenticate', authenticate)
    monkeypatch.setattr(routes, 'bind_authenticated_tenant', authorize)
    get = Mock(return_value=quote)
    monkeypatch.setattr(TenantQuoteRepository, 'get', get)
    monkeypatch.setattr(TenantCatalogRepository, 'items', items)
    app = FastAPI()
    app.include_router(routes.unreleased_tenant_quote_router)
    app.dependency_overrides[get_unreleased_auth_repository] = lambda: AuthSessionRepository(cast(Connection, object()))

    @app.exception_handler(ApiError)
    async def api_error(request: Request, error: ApiError) -> JSONResponse:
        return json_error(request, error.status_code, error.code, error.message)

    with TestClient(app, base_url='https://testserver') as client:
        client.cookies.set(SESSION_COOKIE, 'session')
        client.headers['X-CSRF-Token'] = 'csrf'
        yield client, state, get


def test_owner_download_is_bound_to_server_actor_quote_and_catalogue(boundary: Boundary) -> None:
    client, state, _ = boundary
    quote = state['quote']
    scope = {'expected_revision': quote.revision, 'source_snapshot_sha256': quote.snapshot_sha256}
    url = f'/api/session/organizations/{ORG}/quotes/{quote.quote_id}/training-feedback'
    preview = client.post(url + '/preview', json=scope)
    assert preview.status_code == 200 and preview.headers['Cache-Control'] == 'no-store'
    assert preview.json()['lines'][0]['selected_sku'] == 'FIRST'
    body = {**scope, 'reviewed': True, 'training_consent': True, 'decisions': [
        {'line_number': 1, 'relevant_sku': 'FIRST', 'rejected_skus': ['SECOND'], 'no_match': False}]}
    response = client.post(url, json=body)
    assert response.status_code == 200 and response.headers['Cache-Control'] == 'no-store'
    assert response.json()['reviewer_id'] == str(ACTOR)
    assert all(e['reviewed_by'] == str(ACTOR) for e in response.json()['events'])
    assert all(p['price_rub'] == '0.00' for p in response.json()['catalog'])
    assert 'attachment' in response.headers['Content-Disposition']
    assert client.post(url, json={**body, 'reviewer_id': str(uuid4())}).status_code == 422
    assert client.post(url, json={**body, 'expected_revision': 99}).status_code == 409
    assert client.post(url, json={**body, 'training_consent': False}).status_code == 422


@pytest.mark.parametrize('case,status', [('anonymous', 401), ('csrf', 401), ('revoked', 401),
                                        ('editor', 403), ('other_org', 403)])
def test_unauthorized_context_is_rejected_before_quote_read(boundary: Boundary, case: str, status: int) -> None:
    client, state, get = boundary
    quote = state['quote']
    org = ORG
    if case == 'anonymous': client.cookies.clear()
    if case == 'csrf': client.headers.pop('X-CSRF-Token')
    if case == 'revoked': state['revoked'] = True
    if case == 'editor': state['owner'] = False
    if case == 'other_org': org = uuid4()
    response = client.post(f'/api/session/organizations/{org}/quotes/{quote.quote_id}/training-feedback/preview',
                           json={'expected_revision': 1, 'source_snapshot_sha256': quote.snapshot_sha256})
    assert response.status_code == status
    get.assert_not_called()


def test_late_role_revocation_cannot_release_catalogue_context(boundary: Boundary) -> None:
    client, state, _ = boundary
    quote = state['quote']
    state['late_revoke'] = True
    response = client.post(f'/api/session/organizations/{ORG}/quotes/{quote.quote_id}/training-feedback/preview',
                           json={'expected_revision': 1, 'source_snapshot_sha256': quote.snapshot_sha256})
    assert response.status_code == 403 and 'candidates' not in response.text
