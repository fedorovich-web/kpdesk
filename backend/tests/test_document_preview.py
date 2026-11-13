import json
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import replace
from io import BytesIO
from typing import Any, cast
from unittest.mock import Mock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import Connection
from test_commercial_document import content
from test_company_profile import profile
from test_document_alternatives import saved
from test_quote_pdf import draft
from test_recipient_requisites import document_with_recipient

from kpdesk.api import tenant_document_routes as routes
from kpdesk.api.errors import ApiError, json_error
from kpdesk.api.routes import get_groq_client, get_groq_settings
from kpdesk.api.tenant_session_routes import SESSION_COOKIE, get_unreleased_catalog_engine
from kpdesk.company.storage import CompanyProfileStore
from kpdesk.db.auth_sessions import AuthSessionRepository, SessionDenied
from kpdesk.db.llm_budget import InferenceQuotaExceeded
from kpdesk.db.tenant_access import AuthenticatedUser, TenantAccessDenied
from kpdesk.extraction.groq import GroqSettings
from kpdesk.quotes.alternatives import available_alternatives
from kpdesk.quotes.pdf import QuotePDFError, render_document_preview
from kpdesk.quotes.tenant_storage import TenantQuoteRepository


def test_preview_uses_actual_renderer_with_all_pages_marked_and_does_not_approve() -> None:
    quote = draft()
    before = quote
    raw = render_document_preview(quote, content())
    pages = PdfReader(BytesIO(raw)).pages
    assert all('ПРЕДПРОСМОТР' in page.extract_text() and 'НЕ УТВЕРЖДЕНО' in page.extract_text() for page in pages)
    text = '\n'.join(page.extract_text() for page in pages)
    assert content().narrative.headline in text and '1399.60' in text
    assert 'Закупка' not in text
    assert quote == before and not quote.approved and quote.document is None


def test_preview_refuses_incomplete_or_already_approved_base() -> None:
    with pytest.raises(QuotePDFError):
        render_document_preview(draft(complete=False), content())
    with pytest.raises(QuotePDFError):
        render_document_preview(replace(draft(), approved=True), content())


def test_every_page_of_long_preview_is_marked_without_persisting_document() -> None:
    quote = draft()
    lines = tuple(quote.lines[0].model_copy(update={
        'line_number': index, 'sku': f'LONG-{index:03}',
        'product_name': 'Длинное наименование офисной бумаги ' * 3,
    }) for index in range(1, 21))
    from decimal import Decimal

    extended = replace(quote, lines=lines, total_rub=Decimal('13996.00'))
    pages = PdfReader(BytesIO(render_document_preview(extended, content()))).pages
    assert len(pages) >= 2
    assert all('ПРЕДПРОСМОТР' in page.extract_text() and 'НЕ УТВЕРЖДЕНО' in page.extract_text()
               for page in pages)
    assert extended.document is None and extended.approval is None and not extended.approved


@pytest.fixture
def preview_boundary(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, dict[str, Any]]]:
    quote, products = saved()
    state: dict[str, Any] = {
        'quote': quote, 'organization': uuid4(), 'transaction': False, 'allowed': True,
        'revoked': False, 'profile': None, 'rendered': 0, 'connection': Mock(spec=Connection),
        'alternatives': available_alternatives(quote, products),
    }

    class OfflineEngine:
        @contextmanager
        def begin(self) -> Iterator[Connection]:
            state['transaction'] = True
            try:
                yield cast(Connection, state['connection'])
            finally:
                state['transaction'] = False

    def authenticate(self: object, token: str, *, operation: str, csrf: str | None) -> AuthenticatedUser:
        assert state['transaction'] and operation == 'write'
        if token != 'offline-session' or csrf != 'offline-csrf' or state['revoked']:
            raise SessionDenied()
        return AuthenticatedUser(uuid4())

    def authorize(self: TenantQuoteRepository, *, write: bool = False) -> None:
        assert state['transaction'] and write
        if not state['allowed'] or self.organization_id != state['organization']:
            raise TenantAccessDenied()

    def get_quote(self: TenantQuoteRepository, quote_id: UUID) -> Any:
        assert quote_id == state['quote'].quote_id
        return state['quote']

    def actual_renderer(current: Any, document: Any) -> bytes:
        assert not state['transaction'], 'The authorization/read transaction must end before render'
        state['rendered'] += 1
        return render_document_preview(current, document)

    def forbid_mutation(*args: object, **kwargs: object) -> None:
        raise AssertionError('Preview must never persist, revise or approve a quote')

    monkeypatch.setattr(AuthSessionRepository, 'authenticate', authenticate)
    monkeypatch.setattr(TenantQuoteRepository, '_authorize', authorize)
    monkeypatch.setattr(TenantQuoteRepository, 'get', get_quote)
    monkeypatch.setattr(TenantQuoteRepository, 'resolve_document_alternatives',
                        lambda *args: state['alternatives'])
    monkeypatch.setattr(CompanyProfileStore, 'get', lambda *args, **kwargs: state['profile'])
    monkeypatch.setattr(TenantQuoteRepository, 'approve_document', forbid_mutation)
    monkeypatch.setattr(TenantQuoteRepository, '_append', forbid_mutation)
    monkeypatch.setattr(routes, 'render_document_preview', actual_renderer)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_unreleased_catalog_engine] = lambda: OfflineEngine()

    @app.exception_handler(ApiError)
    async def api_error(request: Request, error: ApiError) -> JSONResponse:
        return json_error(request, error.status_code, error.code, error.message)

    with TestClient(app, base_url='https://testserver') as client:
        client.cookies.set(SESSION_COOKIE, 'offline-session')
        client.headers['X-CSRF-Token'] = 'offline-csrf'
        yield client, state
    state['connection'].execute.assert_not_called()


def preview_request(client: TestClient, state: dict[str, Any], **changes: object) -> Any:
    quote = state['quote']
    body = {'expected_revision': quote.revision, 'source_snapshot_sha256': quote.snapshot_sha256,
            'document': content().model_dump(mode='json'), **changes}
    return client.post(
        f"/api/session/organizations/{state['organization']}/quotes/{quote.quote_id}/document-preview",
        json=body,
    )


def test_endpoint_renders_actual_pdf_and_leaves_quote_unchanged(
    preview_boundary: tuple[TestClient, dict[str, Any]],
) -> None:
    client, state = preview_boundary
    before = state['quote']
    response = preview_request(client, state, document=document_with_recipient().model_dump(mode='json'))
    assert response.status_code == 200, response.text
    assert response.headers['content-type'] == 'application/pdf'
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers['content-disposition'] == 'inline; filename="proposal-preview.pdf"'
    text = '\n'.join(page.extract_text() for page in PdfReader(BytesIO(response.content)).pages)
    assert 'ПРЕДПРОСМОТР' in text and 'НЕ УТВЕРЖДЕНО' in text
    assert 'Реквизиты заказчика' in text and '7707083893' in text
    assert state['quote'] == before and state['quote'].approval is None
    assert state['rendered'] == 1


@pytest.mark.parametrize('failure', ['head', 'hash', 'profile', 'alternative'])
def test_endpoint_rejects_stale_source_profile_and_changed_alternative_before_render(
    preview_boundary: tuple[TestClient, dict[str, Any]], failure: str,
) -> None:
    client, state = preview_boundary
    changes: dict[str, object] = {}
    if failure == 'head':
        changes['expected_revision'] = state['quote'].revision + 1
    elif failure == 'hash':
        changes['source_snapshot_sha256'] = 'b' * 64
    elif failure == 'profile':
        document = content().model_copy(update={'company_profile': profile()})
        changes['document'] = document.model_dump(mode='json')
    else:
        alternative = state['alternatives'][0]
        changed = alternative.model_copy(update={'total_rub': alternative.total_rub + 1})
        document = content().model_copy(update={'alternatives': (changed,)})
        changes['document'] = document.model_dump(mode='json')
    response = preview_request(client, state, **changes)
    assert response.status_code == (422 if failure == 'alternative' else 409), response.text
    assert state['rendered'] == 0
    assert state['quote'].document is None and not state['quote'].approved


@pytest.mark.parametrize('failure,expected', [('cookie', 401), ('csrf', 401), ('revoked', 401),
                                               ('viewer', 403), ('organization', 403)])
def test_endpoint_requires_current_session_csrf_and_write_membership(
    preview_boundary: tuple[TestClient, dict[str, Any]], failure: str, expected: int,
) -> None:
    client, state = preview_boundary
    if failure == 'cookie':
        client.cookies.clear()
    elif failure == 'csrf':
        client.headers.pop('X-CSRF-Token')
    elif failure == 'revoked':
        state['revoked'] = True
    elif failure == 'viewer':
        state['allowed'] = False
    else:
        original = state['organization']
        state['organization'] = uuid4()
        response = client.post(
            f"/api/session/organizations/{original}/quotes/{state['quote'].quote_id}/document-preview",
            json={'expected_revision': state['quote'].revision,
                  'source_snapshot_sha256': state['quote'].snapshot_sha256,
                  'document': content().model_dump(mode='json')},
        )
        assert response.status_code == expected
        assert state['rendered'] == 0
        return
    response = preview_request(client, state)
    assert response.status_code == expected, response.text
    assert state['rendered'] == 0


def test_endpoint_rejects_recipient_name_mismatch_before_render(
    preview_boundary: tuple[TestClient, dict[str, Any]],
) -> None:
    client, state = preview_boundary
    document = document_with_recipient().model_dump(mode='json')
    document['recipient_name'] = 'Другая компания'
    assert preview_request(client, state, document=document).status_code == 422
    assert state['rendered'] == 0


@pytest.mark.parametrize('review_mode', ['accepted', 'corrected', 'rejected', 'quota', 'ledger',
                                       'correction_quota', 'final_review_quota', 'correction_revoked'])
def test_presentation_accounts_for_each_http_call_and_blocks_failed_review(
    preview_boundary: tuple[TestClient, dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
    review_mode: str,
) -> None:
    client, state = preview_boundary
    events: list[str] = []
    active: list[UUID] = []
    attempts = 0
    reservations = 0
    narrative = content().narrative.model_dump()

    def reserve(*args: object, **kwargs: Any) -> UUID:
        nonlocal reservations
        reservations += 1
        assert not active, 'Writer reservation must finish before review reservation'
        assert 0 < kwargs['input_chars'] <= 12000
        if review_mode == 'correction_revoked' and reservations == 3:
            raise SessionDenied()
        if reservations == {'quota': 2, 'correction_quota': 3, 'final_review_quota': 4}.get(review_mode):
            events.append('quota')
            raise InferenceQuotaExceeded('user_quota')
        request_id = uuid4()
        active.append(request_id)
        events.append('reserve')
        return request_id

    def finish(*args: object, **kwargs: Any) -> None:
        assert kwargs['request_id'] == active.pop()
        events.append('finish:' + str(kwargs['succeeded']))
        if review_mode == 'ledger':
            raise RuntimeError('ledger unavailable')

    def provider(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        assert len(active) == 1, 'Every HTTP request needs its own quota reservation'
        events.append('http')
        if attempts % 2:
            payload = narrative
            if attempts == 3:
                payload = {'solution': 'В состав включена офисная бумага формата А4 плотностью 80 г/м².'}
        else:
            payload = {name: {'status': 'supported', 'issues': []} for name in narrative}
            if review_mode not in ('accepted', 'quota', 'ledger') and (attempts == 2 or review_mode == 'rejected'):
                payload['solution'] = {'status': 'unsupported', 'issues': [
                    {'passage_id': 'solution:0', 'reason': 'source_attribution'},
                ]}
        return httpx.Response(200, json={"model": "nvidia/nemotron-3-ultra-550b-a55b:free", 'choices': [{'finish_reason': 'stop',
                             'message': {'content': json.dumps(payload)}}]})

    async def groq_client() -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as transport:
            yield transport

    monkeypatch.setattr(routes, 'reserve_inference', reserve)
    monkeypatch.setattr(routes, 'finish_inference', finish)
    app = cast(FastAPI, client.app)
    app.dependency_overrides[get_groq_client] = groq_client
    app.dependency_overrides[get_groq_settings] = lambda: GroqSettings(test_project_approved=True, api_key='offline-only')
    quote = state['quote']
    response = client.post(
        f"/api/session/organizations/{state['organization']}/quotes/{quote.quote_id}/presentation",
        json={**content().model_dump(mode='json', exclude={'issued_on', 'narrative'}),
              'expected_revision': quote.revision, 'source_snapshot_sha256': quote.snapshot_sha256,
              'consent_to_external_processing': True},
    )
    expected = {'accepted': 200, 'corrected': 200, 'rejected': 422, 'quota': 429, 'ledger': 503,
                'correction_quota': 429, 'final_review_quota': 429, 'correction_revoked': 401}
    assert response.status_code == expected[review_mode], response.text
    expected_attempts = {'accepted': 2, 'corrected': 4, 'rejected': 4, 'quota': 1, 'ledger': 1,
                         'correction_quota': 2, 'final_review_quota': 3, 'correction_revoked': 2}
    assert attempts == expected_attempts[review_mode]
    assert not active
    if review_mode == 'accepted':
        assert events == ['reserve', 'http', 'finish:True', 'reserve', 'http', 'finish:True']
        assert response.json()['requires_human_review'] is True
    elif review_mode == 'rejected':
        assert response.json()['code'] == 'unverified_narrative'
        assert events[-1] == 'finish:False'
        assert 'narrative' not in response.json()
    elif review_mode == 'corrected':
        assert [event for event in events if event.startswith('finish:')] == [
            'finish:True', 'finish:False', 'finish:True', 'finish:True',
        ]


# Explicit offline accounting authority; production requires PostgreSQL.
pytestmark = pytest.mark.usefixtures("mock_llm_operation")
