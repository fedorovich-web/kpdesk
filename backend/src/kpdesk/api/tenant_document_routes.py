"""AI proposal copy and explicit approval of an immutable commercial document."""
from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Annotated

import httpx
from fastapi import APIRouter, Cookie, Depends, Header, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from kpdesk.api.errors import ApiError, llm_error_message
from kpdesk.api.persisted_quote_routes import StoredQuoteResponse
from kpdesk.api.provider_scope import BudgetFactory, get_provider_budget_factory, operation_budget
from kpdesk.api.routes import get_groq_client, get_groq_settings
from kpdesk.api.tenant_quote_routes import _handle_error, _required_key, _store
from kpdesk.api.tenant_session_routes import (
    SESSION_COOKIE,
    get_unreleased_auth_repository,
    get_unreleased_catalog_engine,
)
from kpdesk.company.schema import CompanyProfileSnapshot
from kpdesk.company.storage import CompanyProfileStore
from kpdesk.db.async_inference import InferenceReservations
from kpdesk.db.auth_sessions import AuthSessionRepository, SessionDenied
from kpdesk.db.llm_budget import InferenceQuotaExceeded, finish_inference, reserve_inference
from kpdesk.db.tenant_access import TenantAccessDenied
from kpdesk.llm.client import LLMError, llm_operation
from kpdesk.llm.settings import LLMSettings
from kpdesk.quotes.document import (
    AlternativeSelection,
    CommercialDocument,
    DocumentAlternative,
    ProposalContext,
    ProposalNarrative,
)
from kpdesk.quotes.narrative import generate_proposal_narrative, proposal_facts
from kpdesk.quotes.pdf import render_document_preview
from kpdesk.quotes.storage import QuoteConflict, QuoteNotFound

router = APIRouter(prefix="/api/session/organizations/{organization_id}/quotes")


class PresentationBody(ProposalContext):
    expected_revision: int = Field(ge=1, strict=True)
    source_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    consent_to_external_processing: StrictBool
    company_profile_revision: int | None = Field(default=None, ge=0, le=2147483647, strict=True)
    alternative_selections: tuple[AlternativeSelection, ...] = Field(default=(), max_length=2)


class PresentationPreview(BaseModel):
    quote_id: uuid.UUID
    source_revision: int
    source_snapshot_sha256: str
    narrative: ProposalNarrative
    model: str
    requires_human_review: bool = True
    company_profile: CompanyProfileSnapshot | None = None
    alternatives: tuple[DocumentAlternative, ...] = ()


class AlternativePreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1, strict=True)
    source_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AlternativePreview(BaseModel):
    quote_id: uuid.UUID
    source_revision: int
    source_snapshot_sha256: str
    candidate_limit_per_line: int = 20
    alternatives: tuple[DocumentAlternative, ...]


@router.post("/{quote_id}/alternatives", response_model=AlternativePreview)
def preview_alternatives(
    organization_id: uuid.UUID, quote_id: uuid.UUID, body: AlternativePreviewBody,
    response: Response,
    repository: Annotated[AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function")],
    cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> AlternativePreview:
    store = _store(repository, cookie, organization_id, write=True, csrf=csrf)
    try:
        quote = store.get(quote_id)
        if quote.revision != body.expected_revision or quote.snapshot_sha256 != body.source_snapshot_sha256:
            raise QuoteConflict("stale_revision")
        alternatives = store.document_alternatives(quote)
    except (TenantAccessDenied, QuoteNotFound, QuoteConflict, SQLAlchemyError, RuntimeError, ValueError) as error:
        _handle_error(error)
    response.headers["Cache-Control"] = "no-store"
    return AlternativePreview(quote_id=quote.quote_id, source_revision=quote.revision,
                              source_snapshot_sha256=quote.snapshot_sha256, alternatives=alternatives)


class ApproveDocumentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1, strict=True)
    source_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document: CommercialDocument
    reviewed: StrictBool


class DocumentPreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1, strict=True)
    source_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document: CommercialDocument


@router.post("/{quote_id}/document-preview", response_class=Response)
def preview_document(
    organization_id: uuid.UUID, quote_id: uuid.UUID, body: DocumentPreviewBody,
    engine: Annotated[Engine, Depends(get_unreleased_catalog_engine)],
    cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> Response:
    try:
        with engine.begin() as connection:
            store = _store(AuthSessionRepository(connection), cookie, organization_id, write=True, csrf=csrf)
            store._authorize(write=True)
            quote = store.get(quote_id)
            if quote.revision != body.expected_revision or quote.snapshot_sha256 != body.source_snapshot_sha256:
                raise QuoteConflict("stale_revision")
            store.validate_document_context(quote, body.document)
        raw = render_document_preview(quote, body.document)
    except (TenantAccessDenied, QuoteNotFound, QuoteConflict, SQLAlchemyError, RuntimeError, ValueError) as error:
        _handle_error(error)
    return Response(raw, media_type="application/pdf", headers={
        "Cache-Control": "no-store", "Content-Disposition": 'inline; filename="proposal-preview.pdf"',
        "X-Content-Type-Options": "nosniff",
    })


@router.post("/{quote_id}/presentation", response_model=PresentationPreview)
async def preview_presentation(
    organization_id: uuid.UUID, quote_id: uuid.UUID, body: PresentationBody,
    response: Response,
    engine: Annotated[Engine, Depends(get_unreleased_catalog_engine)],
    provider: Annotated[LLMSettings | None, Depends(get_groq_settings)],
    client: Annotated[httpx.AsyncClient, Depends(get_groq_client)],
    budget_factory: Annotated[BudgetFactory, Depends(get_provider_budget_factory)],
    operation_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> PresentationPreview:
    if not body.consent_to_external_processing:
        raise ApiError(422, "external_processing_consent_required", "Подтвердите передачу контекста в OpenRouter.")
    try:
        with engine.begin() as connection:
            store = _store(AuthSessionRepository(connection), cookie, organization_id, write=True, csrf=csrf)
            store._authorize(write=True)
            quote = store.get(quote_id)
            if quote.revision != body.expected_revision or quote.snapshot_sha256 != body.source_snapshot_sha256:
                raise QuoteConflict("stale_revision")
            alternatives = store.resolve_document_alternatives(quote, body.alternative_selections) \
                if body.alternative_selections else ()
            company_profile = None
            if body.company_profile_revision is not None:
                company_profile = CompanyProfileStore(connection, organization_id, cookie or "", csrf).get()
                if (company_profile.revision if company_profile else 0) != body.company_profile_revision:
                    raise QuoteConflict("stale_company_profile")
            facts = proposal_facts(
                quote, issuer_name=body.issuer_name, recipient_name=body.recipient_name,
                manager_brief=body.manager_brief, commercial_terms=body.commercial_terms,
                company_profile=company_profile,
                alternatives=alternatives,
            )
    except (TenantAccessDenied, QuoteNotFound, QuoteConflict, SQLAlchemyError, RuntimeError, ValueError) as error:
        _handle_error(error)
    if provider is None:
        raise ApiError(503, "provider_not_configured", "Сервис ИИ не настроен. Обратитесь к администратору KPDesk.")
    def reserve_call(input_chars: int) -> uuid.UUID:
        try:
            return reserve_inference(
                engine, session_cookie=cookie, csrf=csrf, organization_id=organization_id,
                model=provider.model, input_chars=input_chars,
            )
        except SessionDenied as error:
            raise ApiError(401, "invalid_session", "Требуется вход в систему.") from error
        except TenantAccessDenied as error:
            raise ApiError(403, "organization_forbidden", "Доступ запрещён.") from error
        except InferenceQuotaExceeded as error:
            status = 403 if error.reason == "forbidden" else 429
            raise ApiError(status, "inference_quota_exceeded", "Генерация недоступна: проверьте права и лимит обращений.") from error
        except (RuntimeError, SQLAlchemyError) as error:
            raise ApiError(503, "tenant_store_unavailable", "Не удалось проверить лимит запросов к ИИ. Попробуйте позже.") from error

    def complete_call(current: uuid.UUID, succeeded: bool) -> None:
        try:
            finish_inference(engine, request_id=current, session_cookie=cookie or "", succeeded=succeeded)
        except (RuntimeError, SQLAlchemyError) as error:
            raise ApiError(503, "quota_ledger_unavailable", "Не удалось подтвердить учёт запроса к ИИ. Проверьте результат перед повторной генерацией.") from error

    reservations = InferenceReservations(reserve_call, complete_call)
    try:
        async with reservations.scope(len(facts)):
            budget = operation_budget(budget_factory, engine, organization_id=organization_id,
                cookie=cookie, csrf=csrf, key=operation_key,
                purpose="presentation:" + str(quote_id), payload=facts, settings=provider)
            async with llm_operation(budget):
                narrative = await generate_proposal_narrative(facts, provider, client,
                    before_followup=reservations.before_followup)
    except LLMError as error:
        semantic_error = error.code in ("unverified_narrative", "review_context_too_large", "correction_context_too_large")
        status = (422 if semantic_error else 503 if error.code == "invalid_provider_schema"
                  else 403 if error.code == "organization_forbidden"
                  else 503 if error.code in {"quota_ledger_unavailable", "synthetic_project_required"}
                  else 429 if error.code in {"rate_limited", "inference_quota_exceeded", "duplicate_inference_in_progress"} else 504 if error.code == "provider_timeout" else 502)
        message = llm_error_message(error.code)
        raise ApiError(status, error.code, message,
                       retry_after_seconds=error.retry_after_seconds if status == 429 else None) from error
    response.headers["Cache-Control"] = "no-store"
    return PresentationPreview(
        quote_id=quote.quote_id, source_revision=quote.revision,
        source_snapshot_sha256=quote.snapshot_sha256, narrative=narrative, model=provider.model,
        company_profile=company_profile,
        alternatives=alternatives,
    )


@router.post("/{quote_id}/approve", response_model=StoredQuoteResponse, status_code=201)
def approve_document(
    organization_id: uuid.UUID, quote_id: uuid.UUID, body: ApproveDocumentBody,
    response: Response,
    repository: Annotated[AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function")],
    cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> StoredQuoteResponse:
    if not body.reviewed:
        raise ApiError(422, "document_review_required", "Проверьте и подтвердите текст и состав предложения.")
    store = _store(repository, cookie, organization_id, write=True, csrf=csrf)
    try:
        quote = store.approve_document(
            quote_id, body.expected_revision, body.source_snapshot_sha256,
            body.document, _required_key(key),
        )
    except (TenantAccessDenied, QuoteNotFound, QuoteConflict, SQLAlchemyError, RuntimeError, ValueError) as error:
        _handle_error(error)
    response.headers["Cache-Control"] = "no-store"
    return StoredQuoteResponse.model_validate(asdict(quote))
