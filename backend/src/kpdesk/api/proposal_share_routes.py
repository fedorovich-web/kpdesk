"""Session-controlled issuance and anonymous PDF-only capability redemption."""
from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Cookie, Depends, Header, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from kpdesk.api.errors import ApiError
from kpdesk.api.tenant_quote_routes import _handle_error, _store
from kpdesk.api.tenant_session_routes import (
    SESSION_COOKIE,
    get_unreleased_auth_repository,
    get_unreleased_catalog_engine,
)
from kpdesk.db.auth_sessions import AuthSessionRepository
from kpdesk.db.tenant_access import TenantAccessDenied
from kpdesk.quotes.sharing import (
    IssuedShare,
    ShareMetadata,
    ShareUnavailable,
    issue_share,
    list_shares,
    public_pdf,
    revoke_share,
    sharing_available,
)
from kpdesk.quotes.storage import QuoteConflict, QuoteNotFound

router = APIRouter()
BASE = '/api/session/organizations/{organization_id}/quotes/{quote_id}/shares'
PRIVATE_HEADERS = {'Cache-Control': 'no-store', 'X-Robots-Tag': 'noindex, nofollow',
                   'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff'}
Repository = Annotated[AuthSessionRepository, Depends(get_unreleased_auth_repository, scope='function')]
CookieToken = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]
CsrfToken = Annotated[str | None, Header(alias='X-CSRF-Token')]


class IssueBody(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_revision: int = Field(ge=1, le=2147483647)
    source_snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    days: Literal[1, 7, 30] = 7


class ShareList(BaseModel):
    available: bool
    shares: list[ShareMetadata]


def require_schema(repository: AuthSessionRepository) -> None:
    if not sharing_available(repository.connection):
        raise ApiError(503, 'proposal_sharing_unavailable', 'Выдача клиентских ссылок пока недоступна.')


@router.get(BASE, response_model=ShareList)
def get_shares(organization_id: uuid.UUID, quote_id: uuid.UUID, repository: Repository,
               cookie: CookieToken = None) -> ShareList:
    store = _store(repository, cookie, organization_id, write=False)
    try:
        store.get(quote_id)
        if not sharing_available(repository.connection):
            return ShareList(available=False, shares=[])
        return ShareList(available=True, shares=list_shares(store, quote_id))
    except (TenantAccessDenied, QuoteNotFound, QuoteConflict, SQLAlchemyError, RuntimeError, ValueError) as error:
        _handle_error(error)


@router.post(BASE, response_model=IssuedShare, status_code=201)
def create_share(organization_id: uuid.UUID, quote_id: uuid.UUID, body: IssueBody,
                 repository: Repository, cookie: CookieToken = None,
                 csrf: CsrfToken = None) -> IssuedShare:
    store = _store(repository, cookie, organization_id, write=True, csrf=csrf)
    try:
        store._authorize(write=True)
        require_schema(repository)
        return issue_share(store, quote_id, body.expected_revision, body.source_snapshot_sha256, body.days)
    except (TenantAccessDenied, QuoteNotFound, QuoteConflict, SQLAlchemyError, RuntimeError, ValueError) as error:
        _handle_error(error)


@router.post(BASE + '/{share_id}/revoke', status_code=204)
def revoke(organization_id: uuid.UUID, quote_id: uuid.UUID, share_id: uuid.UUID,
           repository: Repository, cookie: CookieToken = None, csrf: CsrfToken = None) -> Response:
    store = _store(repository, cookie, organization_id, write=True, csrf=csrf)
    try:
        store._authorize(write=True)
        require_schema(repository)
        revoke_share(store, quote_id, share_id)
    except ShareUnavailable as error:
        raise ApiError(404, 'proposal_unavailable', 'Ссылка недоступна.') from error
    except (TenantAccessDenied, QuoteNotFound, QuoteConflict, SQLAlchemyError, RuntimeError, ValueError) as error:
        _handle_error(error)
    return Response(status_code=204, headers=PRIVATE_HEADERS)


@router.post('/api/public/proposal/pdf', response_class=Response)
def read_public_pdf(engine: Annotated[Engine, Depends(get_unreleased_catalog_engine)],
                    token: Annotated[str | None, Header(alias='X-Proposal-Token')] = None) -> Response:
    try:
        with engine.begin() as connection:
            raw = public_pdf(connection, token)
    except ShareUnavailable as error:
        raise ApiError(404, 'proposal_unavailable', 'Ссылка недоступна. Обратитесь к отправителю КП.') from error
    except (SQLAlchemyError, RuntimeError, ValueError) as error:
        raise ApiError(503, 'proposal_temporarily_unavailable', 'Не удалось открыть КП. Попробуйте позже.') from error
    return Response(raw, media_type='application/pdf', headers={**PRIVATE_HEADERS,
        'Content-Disposition': 'inline; filename="KPDesk-proposal.pdf"'})
