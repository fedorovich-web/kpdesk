"""Explicit opt-in organization-bound quote endpoints with immutable prices.

Only mounted in the HTTPS-only tenant app (or isolated tests). Not published
through the default legacy app. The injected repository owns an open
transaction and finishes COMMIT or ROLLBACK before the response bytes.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime
from typing import Annotated, NoReturn

from fastapi import APIRouter, Cookie, Depends, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql.elements import TextClause

from kpdesk.api.errors import ApiError, quote_conflict_message, quote_pdf_error_message
from kpdesk.api.persisted_quote_routes import StoredQuoteResponse
from kpdesk.api.tenant_session_routes import (
    SESSION_COOKIE,
    _required_session,
    get_unreleased_auth_repository,
)
from kpdesk.catalog.tenant_storage import TenantCatalogRepository
from kpdesk.db.auth_sessions import AuthSessionRepository
from kpdesk.db.authenticated_tenant import bind_authenticated_tenant
from kpdesk.db.tenant_access import TenantAccessDenied
from kpdesk.extraction.schema import ExtractedRequest
from kpdesk.matching.feedback_bundle import (
    FeedbackBundle,
    FeedbackExportRequest,
    FeedbackScope,
    build_feedback_bundle,
    feedback_context,
)
from kpdesk.matching.feedback_storage import FeedbackHistory, FeedbackSummary, TrainingFeedbackStore
from kpdesk.quotes.instructions import (
    ConfirmedInstructions,
    InstructionProposal,
    propose_instructions,
)
from kpdesk.quotes.pdf import QuotePDFError, render_draft_pdf
from kpdesk.quotes.review_audit import (
    QuoteReviewDenied,
    ReviewEvidence,
    attest_quote_review,
)
from kpdesk.quotes.storage import ManagerSkuSelection, QuoteConflict, QuoteNotFound, StoredQuote
from kpdesk.quotes.tenant_storage import TenantQuoteRepository

unreleased_tenant_quote_router = APIRouter(
    prefix="/api/session/organizations/{organization_id}/quotes",
)


class TenantCreateQuote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    catalog_version_id: uuid.UUID
    extracted_request: ExtractedRequest
    manager_selections: list[ManagerSkuSelection] | None = Field(default=None, max_length=20)
    review: ReviewEvidence | None = None
    instructions: ConfirmedInstructions | None = None


class TenantReviseQuote(TenantCreateQuote):
    expected_revision: int = Field(ge=1)


class TenantQuoteListItem(BaseModel):
    quote_id: uuid.UUID
    revision: int
    total_rub: str | None
    ready_for_draft: bool
    approved: bool


class TenantQuoteList(BaseModel):
    items: list[TenantQuoteListItem]
    next_cursor: uuid.UUID | None = None


# Avoid joining every historical document before the page LIMIT.
# The fallback preserves the original INNER JOIN semantics when one of the
# newest document heads has no corresponding committed revision.
def _quote_list_sql(
    limit: int, *, before: bool = False, include_approval: bool = False,
) -> TextClause:
    continuation = (
        'AND (d.created_at,d.id) < (:before_created_at,:before_id)' if before else ''
    )
    approval = (
        ",COALESCE(r.snapshot->'approved' = 'true'::jsonb,false) AS approved"
        if include_approval else ''
    )
    approval_column = ',approved' if include_approval else ''
    return text(f"""
    WITH heads AS MATERIALIZED (
        SELECT id,organization_id,current_revision,created_at
        FROM public.quote_documents d
        WHERE organization_id=:org
        {continuation}
        ORDER BY created_at DESC,id DESC
        LIMIT {limit}
    ),
    fast AS MATERIALIZED (
        SELECT h.id,h.current_revision,r.total_rub,r.is_complete,h.created_at{approval}
        FROM heads h
        JOIN public.quote_revisions r
          ON r.organization_id=h.organization_id
         AND r.quote_id=h.id AND r.number=h.current_revision
    ),
    fallback AS (
        SELECT d.id,d.current_revision,r.total_rub,r.is_complete,d.created_at{approval}
        FROM public.quote_documents d
        JOIN public.quote_revisions r
          ON r.organization_id=d.organization_id
         AND r.quote_id=d.id AND r.number=d.current_revision
        WHERE d.organization_id=:org
          {continuation}
          AND (SELECT count(*) FROM fast) < {limit}
    )
    SELECT id,current_revision,total_rub,is_complete{approval_column}
    FROM (
        SELECT id,current_revision,total_rub,is_complete,created_at{approval_column}
        FROM fast WHERE (SELECT count(*) FROM fast) = {limit}
        UNION ALL
        SELECT id,current_revision,total_rub,is_complete,created_at{approval_column}
        FROM fallback
    ) selected
    ORDER BY created_at DESC,id DESC
    LIMIT {limit}
""")


# Keep the historical 50-row statement available to the existing SQL benchmark.
_TENANT_QUOTE_LIST_SQL = _quote_list_sql(50)
_TENANT_QUOTE_PAGE_SQL = _quote_list_sql(51, include_approval=True)
_TENANT_QUOTE_CONTINUATION_SQL = _quote_list_sql(51, before=True, include_approval=True)


def _required_key(value: str | None) -> str:
    if (
        value is None or not 1 <= len(value) <= 128
        or any(not 33 <= ord(char) <= 126 for char in value)
    ):
        raise ApiError(422, "invalid_idempotency_key", "Не удалось определить запрос сохранения. Проверьте сохранённое КП перед повторной попыткой.")
    return value


def _handle_error(error: Exception) -> NoReturn:
    if isinstance(error, TenantAccessDenied):
        raise ApiError(403, "organization_forbidden", "У вас нет доступа к данным этой организации.") from error
    if isinstance(error, QuoteNotFound):
        raise ApiError(404, "quote_not_found", "КП или его версия не найдены.") from error
    if isinstance(error, QuoteConflict):
        feedback_messages = {
            'feedback_key_conflict': 'Ключ сохранения уже использован для другой оценки.',
            'feedback_withdrawn': 'Согласие на использование этой записи отозвано.',
            'feedback_storage_limit': 'Достигнут лимит хранения оценок организации.',
            'feedback_cursor_invalid': 'История изменилась. Загрузите её сначала.',
        }
        if str(error) in feedback_messages:
            raise ApiError(409, str(error), feedback_messages[str(error)]) from error
        if str(error) == "stale_company_profile":
            raise ApiError(409, "stale_company_profile", "Сведения компании изменились. Обновите их и повторите генерацию текста.") from error
        raise ApiError(409, str(error), quote_conflict_message(str(error))) from error
    if isinstance(error, QuoteReviewDenied):
        raise ApiError(
            409, "review_attestation_denied",
            "Не удалось подтвердить проверку этой версии КП. Заново проверьте заявку и выбранные товары.",
        ) from error
    if isinstance(error, (ValueError,)):
        alternative_errors = {
            'feedback_source_request_required': 'Для разметки нужна сохранённая исходная заявка.',
            'feedback_single_catalog_required': 'Разметка пока доступна для КП из одного прайса.',
            'feedback_complete_request_required': 'Сначала уточните обязательные параметры заявки.',
            'feedback_catalog_limit': 'Для выгрузки нужен прайс от 1 до 5000 товаров.',
            'feedback_review_required': 'Подтвердите оценки и использование разметки для обучения.',
            'feedback_candidate_invalid': 'Оценивать можно только товары из прайса этой редакции.',
            'feedback_positive_incompatible': 'Подходящий товар противоречит обязательным параметрам заявки.',
            'feedback_line_mismatch': 'Проверьте оценки всех строк сохранённой заявки.',
            'feedback_too_large': 'Файл разметки превышает допустимый размер.',
            'feedback_no_match_requires_full_catalog': 'Отсутствие подходящего товара нужно подтвердить проверкой всего прайса.',
            "multi_source_alternatives_unavailable": "Дополнительные варианты пока доступны для КП из одного прайса. Можно продолжить с сохранённым составом.",
            "alternative_source_request_unavailable": "В этой старой ревизии нет сохранённой заявки для подбора вариантов. Можно продолжить с основным составом.",
            "alternative_snapshot_mismatch": "Данные варианта изменены. Заново подберите варианты и проверьте состав и суммы.",
            "presentation_context_too_large": "Контекст предложения слишком большой. Сократите задание или число включённых вариантов.",
        }
        if str(error) in alternative_errors:
            raise ApiError(422, str(error), alternative_errors[str(error)]) from error
        raise ApiError(422, "invalid_quote", "Данные КП не прошли проверку. Проверьте заявку, выбранные товары и условия предложения.") from error
    if isinstance(error, (SQLAlchemyError, RuntimeError)):
        raise ApiError(503, "quote_store_unavailable", "Не удалось получить или сохранить данные КП. Проверьте список предложений перед повторной попыткой.") from error
    raise error


def _store(
    repository: AuthSessionRepository,
    session_cookie: str | None,
    organization_id: uuid.UUID,
    *,
    write: bool,
    csrf: str | None = None,
) -> TenantQuoteRepository:
    verified = _required_session(
        repository, session_cookie, csrf=csrf, write=write,
    )
    return TenantQuoteRepository(
        repository.connection, verified, organization_id,
        session_token=session_cookie or "", csrf_token=csrf,
    )


def _feedback_source(
    repository: AuthSessionRepository, cookie: str | None, csrf: str | None,
    org: uuid.UUID, quote_id: uuid.UUID, body: FeedbackScope,
) -> tuple[TenantQuoteRepository, StoredQuote]:
    store = _store(repository, cookie, org, write=True, csrf=csrf)
    bind_authenticated_tenant(repository.connection, session_token=cookie or '',
                              organization_id=org, operation='manage', csrf=csrf)
    quote = store.get(quote_id)
    if quote.revision != body.expected_revision or quote.snapshot_sha256 != body.source_snapshot_sha256:
        raise QuoteConflict('stale_revision')
    return store, quote


@unreleased_tenant_quote_router.post('/{quote_id}/training-feedback/preview')
def preview_training_feedback(
    organization_id: uuid.UUID, quote_id: uuid.UUID, body: FeedbackScope, response: Response,
    repository: Annotated[AuthSessionRepository, Depends(get_unreleased_auth_repository, scope='function')],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias='X-CSRF-Token')] = None,
) -> dict[str, object]:
    try:
        store, quote = _feedback_source(repository, session_cookie, csrf, organization_id, quote_id, body)
        products = TenantCatalogRepository(repository.connection, store.identity, organization_id,
                                            session_token=session_cookie, csrf_token=csrf).items(quote.catalog_version_id)
        result = feedback_context(quote, products)
        bind_authenticated_tenant(repository.connection, session_token=session_cookie or '',
                                  organization_id=organization_id, operation='manage', csrf=csrf)
    except (TenantAccessDenied, QuoteNotFound, QuoteConflict, SQLAlchemyError, RuntimeError, ValueError) as error:
        _handle_error(error)
    response.headers['Cache-Control'] = 'no-store'
    return result


def _feedback_history_store(
    repository: AuthSessionRepository, cookie: str | None, csrf: str | None,
    org: uuid.UUID, *, write: bool,
) -> TrainingFeedbackStore:
    _required_session(repository, cookie, write=write, csrf=csrf)
    bind_authenticated_tenant(repository.connection, session_token=cookie or '',
                              organization_id=org, operation='manage', csrf=csrf)
    return TrainingFeedbackStore(repository.connection, org, cookie or '', csrf)


@unreleased_tenant_quote_router.post('/{quote_id}/training-feedback/history', response_model=FeedbackSummary)
def save_training_feedback(
    organization_id: uuid.UUID, quote_id: uuid.UUID, body: FeedbackExportRequest, response: Response,
    repository: Annotated[AuthSessionRepository, Depends(get_unreleased_auth_repository, scope='function')],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias='X-CSRF-Token')] = None,
    idempotency_key: Annotated[str | None, Header(alias='Idempotency-Key')] = None,
) -> FeedbackSummary:
    try:
        key = _required_key(idempotency_key)
        history = _feedback_history_store(repository, session_cookie, csrf, organization_id, write=True)
        existing = history.retry(quote_id, body, key)
        if existing is not None:
            response.headers['Cache-Control'] = 'no-store'
            return existing
        store, quote = _feedback_source(repository, session_cookie, csrf, organization_id, quote_id, body)
        products = TenantCatalogRepository(repository.connection, store.identity, organization_id,
                                            session_token=session_cookie, csrf_token=csrf).items(quote.catalog_version_id)
        bundle = build_feedback_bundle(quote, products, body, organization_id=organization_id,
                                       reviewer_id=store.identity.user_id)
        bind_authenticated_tenant(repository.connection, session_token=session_cookie or '',
                                  organization_id=organization_id, operation='manage', csrf=csrf)
        result = history.save(bundle, body, key)
    except (TenantAccessDenied, QuoteNotFound, QuoteConflict, SQLAlchemyError, RuntimeError, ValueError) as error:
        _handle_error(error)
    response.headers['Cache-Control'] = 'no-store'
    return result


@unreleased_tenant_quote_router.get('/{quote_id}/training-feedback/history', response_model=FeedbackHistory)
def training_feedback_history(
    organization_id: uuid.UUID, quote_id: uuid.UUID, response: Response,
    repository: Annotated[AuthSessionRepository, Depends(get_unreleased_auth_repository, scope='function')],
    before: uuid.UUID | None = None,
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias='X-CSRF-Token')] = None,
) -> FeedbackHistory:
    try:
        result = _feedback_history_store(repository, session_cookie, csrf, organization_id, write=False).history(quote_id, before)
    except (TenantAccessDenied, QuoteNotFound, QuoteConflict, SQLAlchemyError, RuntimeError, ValueError) as error:
        _handle_error(error)
    response.headers['Cache-Control'] = 'no-store'
    return result


@unreleased_tenant_quote_router.get('/{quote_id}/training-feedback/history/{feedback_id}', response_model=FeedbackBundle)
def download_saved_training_feedback(
    organization_id: uuid.UUID, quote_id: uuid.UUID, feedback_id: uuid.UUID, response: Response,
    repository: Annotated[AuthSessionRepository, Depends(get_unreleased_auth_repository, scope='function')],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias='X-CSRF-Token')] = None,
) -> FeedbackBundle:
    try:
        result = _feedback_history_store(repository, session_cookie, csrf, organization_id, write=False).get(quote_id, feedback_id)
    except (TenantAccessDenied, QuoteNotFound, QuoteConflict, SQLAlchemyError, RuntimeError, ValueError) as error:
        _handle_error(error)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Content-Disposition'] = 'attachment; filename="kpdesk-manager-feedback.json"'
    return result


@unreleased_tenant_quote_router.post('/{quote_id}/training-feedback/history/{feedback_id}/withdraw', response_model=FeedbackSummary)
def withdraw_training_feedback(
    organization_id: uuid.UUID, quote_id: uuid.UUID, feedback_id: uuid.UUID, response: Response,
    repository: Annotated[AuthSessionRepository, Depends(get_unreleased_auth_repository, scope='function')],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias='X-CSRF-Token')] = None,
) -> FeedbackSummary:
    try:
        result = _feedback_history_store(repository, session_cookie, csrf, organization_id, write=True).withdraw(quote_id, feedback_id)
    except (TenantAccessDenied, QuoteNotFound, QuoteConflict, SQLAlchemyError, RuntimeError, ValueError) as error:
        _handle_error(error)
    response.headers['Cache-Control'] = 'no-store'
    return result


@unreleased_tenant_quote_router.post('/{quote_id}/training-feedback', response_model=FeedbackBundle)
def export_training_feedback(
    organization_id: uuid.UUID, quote_id: uuid.UUID, body: FeedbackExportRequest, response: Response,
    repository: Annotated[AuthSessionRepository, Depends(get_unreleased_auth_repository, scope='function')],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias='X-CSRF-Token')] = None,
) -> FeedbackBundle:
    try:
        store, quote = _feedback_source(repository, session_cookie, csrf, organization_id, quote_id, body)
        products = TenantCatalogRepository(repository.connection, store.identity, organization_id,
                                            session_token=session_cookie, csrf_token=csrf).items(quote.catalog_version_id)
        result = build_feedback_bundle(quote, products, body, organization_id=organization_id,
                                       reviewer_id=store.identity.user_id)
        bind_authenticated_tenant(repository.connection, session_token=session_cookie or '',
                                  organization_id=organization_id, operation='manage', csrf=csrf)
    except (TenantAccessDenied, QuoteNotFound, QuoteConflict, SQLAlchemyError, RuntimeError, ValueError) as error:
        _handle_error(error)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Content-Disposition'] = 'attachment; filename="kpdesk-manager-feedback.json"'
    return result


class TenantInstructionPreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)


@unreleased_tenant_quote_router.post(
    "/instructions/preview", response_model=InstructionProposal,
)
def preview_tenant_quote_instructions(
    organization_id: uuid.UUID,
    body: TenantInstructionPreviewBody,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> InstructionProposal:
    store = _store(
        repository, session_cookie, organization_id, write=True, csrf=csrf,
    )
    try:
        store._authorize(write=True)
        proposal = propose_instructions(body.text)
    except (TenantAccessDenied, SQLAlchemyError, RuntimeError, ValueError) as error:
        _handle_error(error)
    return proposal


@unreleased_tenant_quote_router.get(
    "", response_model=TenantQuoteList,
)
def list_tenant_quotes(
    organization_id: uuid.UUID,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    response: Response,
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    cursor: Annotated[uuid.UUID | None, Query()] = None,
) -> TenantQuoteList:
    store = _store(repository, session_cookie, organization_id, write=False)
    try:
        store._authorize(write=False)
        if cursor is None:
            rows = repository.connection.execute(
                _TENANT_QUOTE_PAGE_SQL, {"org": organization_id},
            ).all()
        else:
            anchor = repository.connection.execute(text("""
                SELECT id,created_at FROM public.quote_documents
                WHERE organization_id=:org AND id=:cursor
            """), {"org": organization_id, "cursor": cursor}).one_or_none()
            if anchor is None:
                raise ApiError(422, "invalid_quote_cursor", "Обновите список КП и повторите загрузку.")
            rows = repository.connection.execute(
                _TENANT_QUOTE_CONTINUATION_SQL,
                {"org": organization_id, "before_created_at": anchor.created_at,
                 "before_id": anchor.id},
            ).all()
    except (TenantAccessDenied, SQLAlchemyError, RuntimeError) as error:
        _handle_error(error)
    response.headers["Cache-Control"] = "no-store"
    return TenantQuoteList(items=[
        TenantQuoteListItem(
            quote_id=row.id, revision=row.current_revision,
            total_rub=str(row.total_rub) if row.total_rub is not None else None,
            ready_for_draft=row.is_complete,
            approved=row.approved,
        ) for row in rows[:50]
    ], next_cursor=rows[49].id if len(rows) > 50 else None)


@unreleased_tenant_quote_router.post(
    "", status_code=201, response_model=StoredQuoteResponse,
)
def create_tenant_quote(
    organization_id: uuid.UUID,
    response: Response,
    body: TenantCreateQuote,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> StoredQuoteResponse:
    key = _required_key(idempotency_key)
    store = _store(
        repository, session_cookie, organization_id, write=True, csrf=csrf,
    )
    try:
        quote = store.create(
            body.catalog_version_id, body.extracted_request, key,
            body.manager_selections, body.instructions,
        )
        if body.review is not None:
            attest_quote_review(
                repository.connection,
                organization_id=organization_id, quote_id=quote.quote_id,
                revision=quote.revision, catalog_id=body.catalog_version_id,
                extracted=body.extracted_request, expected_revision=None,
                evidence=body.review,
                selections=body.manager_selections, instructions=body.instructions,
            )
    except (TenantAccessDenied, QuoteNotFound, QuoteConflict, SQLAlchemyError,
            RuntimeError, ValueError) as error:
        _handle_error(error)
    response.headers["Cache-Control"] = "no-store"
    return StoredQuoteResponse.model_validate(asdict(quote))


@unreleased_tenant_quote_router.post(
    "/{quote_id}/revisions", status_code=201, response_model=StoredQuoteResponse,
)
def revise_tenant_quote(
    organization_id: uuid.UUID,
    quote_id: uuid.UUID,
    response: Response,
    body: TenantReviseQuote,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> StoredQuoteResponse:
    key = _required_key(idempotency_key)
    store = _store(
        repository, session_cookie, organization_id, write=True, csrf=csrf,
    )
    try:
        quote = store.revise(
            quote_id, body.expected_revision, body.catalog_version_id,
            body.extracted_request, key, body.manager_selections,
            body.instructions,
        )
        if body.review is not None:
            attest_quote_review(
                repository.connection,
                organization_id=organization_id, quote_id=quote.quote_id,
                revision=quote.revision, catalog_id=body.catalog_version_id,
                extracted=body.extracted_request,
                expected_revision=body.expected_revision, evidence=body.review,
                selections=body.manager_selections, instructions=body.instructions,
            )
    except (TenantAccessDenied, QuoteNotFound, QuoteConflict, SQLAlchemyError,
            RuntimeError, ValueError) as error:
        _handle_error(error)
    response.headers["Cache-Control"] = "no-store"
    return StoredQuoteResponse.model_validate(asdict(quote))


def _read_tenant_quote(
    store: TenantQuoteRepository, quote_id: uuid.UUID, revision: int | None,
) -> StoredQuote:
    try:
        return store.get(quote_id, revision)
    except (TenantAccessDenied, QuoteNotFound, QuoteConflict, SQLAlchemyError,
            RuntimeError, ValueError) as error:
        _handle_error(error)


class QuoteReviewAuditResponse(BaseModel):
    reviewer_user_id: uuid.UUID
    reviewed_at: datetime
    extraction_id: uuid.UUID
    source_sha256: str
    preview_sha256: str
    reviewed_request_sha256: str
    reviewed_line_hashes: list[str]


@unreleased_tenant_quote_router.get(
    "/{quote_id}/reviews/{revision}", response_model=QuoteReviewAuditResponse,
)
def get_quote_review_audit(
    organization_id: uuid.UUID,
    quote_id: uuid.UUID,
    revision: int,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    response: Response,
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> QuoteReviewAuditResponse:
    store = _store(repository, session_cookie, organization_id, write=False)
    try:
        store._authorize(write=False)
        quote = store.get(quote_id, revision)
        row = repository.connection.execute(text("""
            SELECT reviewer_user_id,reviewed_at,extraction_id,source_sha256,
                   preview_sha256,reviewed_request_sha256,reviewed_line_hashes
            FROM public.kpdesk_quote_review_get(:org,:quote,:revision)
        """), {
            "org": organization_id, "quote": quote.quote_id,
            "revision": quote.revision,
        }).mappings().one_or_none()
    except (TenantAccessDenied, QuoteNotFound, QuoteConflict, SQLAlchemyError,
            RuntimeError, ValueError) as error:
        _handle_error(error)
    if row is None:
        raise ApiError(404, "review_not_found", "Для этой версии КП нет сохранённого подтверждения проверки менеджером.")
    response.headers["Cache-Control"] = "no-store"
    return QuoteReviewAuditResponse.model_validate(dict(row))


class ManagerDecisionRow(BaseModel):
    line_number: int = Field(ge=1, le=20)
    sku: str
    catalog_version_id: uuid.UUID
    approved_by_user_id: uuid.UUID
    recorded_at: datetime


class ManagerDecisionHistory(BaseModel):
    quote_id: uuid.UUID
    revision: int = Field(ge=1)
    items: list[ManagerDecisionRow]


@unreleased_tenant_quote_router.get(
    "/{quote_id}/manager-decisions/{revision}",
    response_model=ManagerDecisionHistory,
)
def get_quote_manager_decisions(
    organization_id: uuid.UUID,
    quote_id: uuid.UUID,
    revision: int,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    response: Response,
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> ManagerDecisionHistory:
    store = _store(repository, session_cookie, organization_id, write=False)
    quote = _read_tenant_quote(store, quote_id, revision)
    try:
        rows = repository.connection.execute(text("""
            SELECT line_number,sku,catalog_version_id,
                   approved_by_user_id,recorded_at
            FROM public.kpdesk_quote_manager_decisions_get(
                :org,:quote,:revision
            )
        """), {
            "org": organization_id,
            "quote": quote.quote_id,
            "revision": quote.revision,
        }).mappings().all()
    except (TenantAccessDenied, SQLAlchemyError, RuntimeError) as error:
        _handle_error(error)
    response.headers["Cache-Control"] = "no-store"
    return ManagerDecisionHistory(
        quote_id=quote.quote_id,
        revision=quote.revision,
        items=[ManagerDecisionRow.model_validate(dict(row)) for row in rows],
    )


@unreleased_tenant_quote_router.get("/{quote_id}", response_model=StoredQuoteResponse)
def get_tenant_quote(
    organization_id: uuid.UUID,
    quote_id: uuid.UUID,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    response: Response,
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    revision: Annotated[int | None, Query(ge=1)] = None,
) -> StoredQuoteResponse:
    store = _store(repository, session_cookie, organization_id, write=False)
    quote = _read_tenant_quote(store, quote_id, revision)
    response.headers["Cache-Control"] = "no-store"
    return StoredQuoteResponse.model_validate(asdict(quote))


@unreleased_tenant_quote_router.get("/{quote_id}/pdf")
def get_tenant_quote_pdf(
    organization_id: uuid.UUID,
    quote_id: uuid.UUID,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    revision: Annotated[int | None, Query(ge=1)] = None,
) -> Response:
    store = _store(repository, session_cookie, organization_id, write=False)
    quote = _read_tenant_quote(store, quote_id, revision)
    try:
        content = render_draft_pdf(quote)
    except QuotePDFError as error:
        status = 409 if str(error) in (
            "incomplete_draft", "inconsistent_quote_total",
        ) else 503
        raise ApiError(status, str(error), quote_pdf_error_message(str(error))) from error
    return Response(
        content=content, media_type="application/pdf",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": (
                f'attachment; filename="KPDesk-Draft-{quote.quote_id}-r{quote.revision}.pdf"'
            ),
        },
    )
