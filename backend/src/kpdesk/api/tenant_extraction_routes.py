"""Consent-gated text extraction for the opt-in, HTTPS-only tenant app.

The provider sees only the explicitly submitted request text. Authentication,
organization write rights and CSRF are revalidated BEFORE any external call.
Output is a transient, unapproved preview, never a quote or catalog write.
"""
from __future__ import annotations

import logging
import uuid
from functools import partial
from typing import Annotated

import httpx
from fastapi import APIRouter, Cookie, Depends, Header, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from kpdesk.api.errors import ApiError, llm_error_message
from kpdesk.api.provider_scope import BudgetFactory, get_provider_budget_factory, operation_budget
from kpdesk.api.routes import (
    GroqModel,
    get_groq_client,
    get_groq_settings,
)
from kpdesk.api.tenant_session_routes import (
    SESSION_COOKIE,
    _required_session,
    get_unreleased_auth_repository,
    get_unreleased_catalog_engine,
)
from kpdesk.async_work import run_thread, settle_thread
from kpdesk.catalog.tenant_storage import TenantCatalogRepository
from kpdesk.db.async_inference import InferenceReservations
from kpdesk.db.auth_sessions import AuthSessionRepository, SessionDenied
from kpdesk.db.llm_budget import (
    InferenceQuotaExceeded,
    finish_inference,
    reserve_inference,
    seal_inference_preview,
)
from kpdesk.db.llm_cache import (
    cache_secret,
    claim_cached_extraction,
    decrypt_cached_payload,
    extraction_hmac,
    publish_cached_extraction,
    release_cached_extraction,
)
from kpdesk.db.tenant_access import TenantAccessDenied
from kpdesk.extraction.analysis import RequestAnalysis, analyze_request
from kpdesk.extraction.grounding import validate_extraction_grounding
from kpdesk.extraction.schema import ExtractedRequest
from kpdesk.extraction.service import extract_request
from kpdesk.llm.client import LLMError, llm_operation
from kpdesk.llm.settings import LLMSettings
from kpdesk.quotes.instructions import InstructionProposal, propose_instructions

_LOG = logging.getLogger("kpdesk.llm_cache")

router = APIRouter(prefix="/api/session/organizations/{organization_id}/requests")


class TenantExtractText(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    text: str = Field(min_length=1, max_length=12_000)
    consent_to_external_processing: bool
    model: GroqModel | None = None


class TenantExtractionPreview(BaseModel):
    extraction_id: uuid.UUID
    extracted_request: ExtractedRequest
    requires_human_review: bool = True
    source: str = "openrouter"
    approved: bool = False
    cache_hit: bool = False
    instruction_proposal: InstructionProposal | None = None
    analysis: RequestAnalysis | None = None


class TenantAnalysisBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    extracted_request: ExtractedRequest


@router.post("/analyze", response_model=RequestAnalysis)
def analyze_tenant_request(
    organization_id: uuid.UUID, body: TenantAnalysisBody, response: Response,
    repository: Annotated[AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function")],
    cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> RequestAnalysis:
    identity = _required_session(repository, cookie, csrf=csrf, write=True)
    try:
        # Authorization only: no active catalog or external provider is required.
        TenantCatalogRepository(repository.connection, identity, organization_id,
                                session_token=cookie, csrf_token=csrf)._authorize("write")
    except TenantAccessDenied as error:
        raise ApiError(403, "organization_forbidden", "Нет доступа к данным этой организации.") from error
    except SQLAlchemyError as error:
        raise ApiError(503, "analysis_unavailable", "Не удалось проверить права организации.") from error
    response.headers["Cache-Control"] = "no-store"
    return analyze_request(body.extracted_request)


@router.post("/extract", response_model=TenantExtractionPreview)
async def extract_tenant_request(
    organization_id: uuid.UUID,
    body: TenantExtractText,
    response: Response,
    tenant_engine: Annotated[
        Engine, Depends(get_unreleased_catalog_engine),
    ],
    provider: Annotated[LLMSettings | None, Depends(get_groq_settings)],
    client: Annotated[httpx.AsyncClient, Depends(get_groq_client)],
    budget_factory: Annotated[BudgetFactory, Depends(get_provider_budget_factory)],
    operation_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> TenantExtractionPreview:
    # Explicit per-request consent: never transmit text because a checkbox
    # happened to be checked earlier in a different organization or document.
    if not body.consent_to_external_processing:
        raise ApiError(
            422, "external_processing_consent_required",
            "Для передачи текста внешнему LLM требуется явное согласие.",
        )
    if not body.text.strip():
        raise ApiError(422, "invalid_request_text", "Введите текст заявки.")

    if provider is None:
        raise ApiError(503, "provider_not_configured", "Сервис ИИ не настроен. Обратитесь к администратору KPDesk.")
    if body.model is not None:
        provider = provider.model_copy(update={"model": body.model})

    def reserve_call(input_chars: int) -> uuid.UUID:
        try:
            return reserve_inference(
                tenant_engine, session_cookie=session_cookie, csrf=csrf,
                organization_id=organization_id, model=provider.model,
                input_chars=input_chars,
            )
        except SessionDenied as error:
            raise ApiError(401, "invalid_session", "Для продолжения войдите в KPDesk.") from error
        except TenantAccessDenied as error:
            raise ApiError(403, "organization_forbidden", "Нет доступа к данным этой организации.") from error
        except InferenceQuotaExceeded as error:
            if error.reason == "forbidden":
                raise ApiError(403, "organization_forbidden", "Нет доступа к данным этой организации.") from error
            if error.reason in {
                "user_quota", "organization_quota",
                "user_busy", "organization_busy",
            }:
                raise ApiError(
                    429, "inference_quota_exceeded",
                    "Лимит LLM-запросов или параллельных обращений исчерпан.",
                ) from error
            raise ApiError(422, "invalid_request", "Не удалось проверить заявку. Проверьте исходный текст.") from error
        except (RuntimeError, SQLAlchemyError) as error:
            raise ApiError(
                503, "tenant_store_unavailable", "Не удалось проверить лимит запросов к ИИ. Попробуйте позже."
            ) from error

    def complete_call(current: uuid.UUID, succeeded: bool) -> None:
        try:
            finish_inference(tenant_engine, request_id=current,
                session_cookie=session_cookie or "", succeeded=succeeded)
        except (RuntimeError, SQLAlchemyError) as error:
            raise ApiError(503, "quota_ledger_unavailable",
                "Не удалось подтвердить учёт запроса к ИИ. Проверьте результат перед повторной генерацией.") from error

    reservations = InferenceReservations(reserve_call, complete_call)
    claimed = cache_hit = published = False
    request_hmac: str | None = None
    reservation: uuid.UUID | None = None
    try:
        async with reservations.scope(len(body.text)) as reservation:
            # An admitted replay remains a unique reviewable ledger attempt, not a
            # reused audit identity. HMAC secret is separate from provider credentials.
            secret = cache_secret()
            request_hmac = (
                extraction_hmac(secret, text_source=body.text, model=provider.model)
                if secret is not None else None
            )
            if request_hmac is not None:
                try:
                    claim, cancellation = await settle_thread(partial(claim_cached_extraction,
                        tenant_engine, organization_id=organization_id,
                        attempt_id=reservation, session_cookie=session_cookie or "",
                        csrf=csrf or "", request_hmac=request_hmac, model=provider.model,
                    ))
                    claimed = claim.status == "miss"
                    if cancellation is not None:
                        raise cancellation
                except (RuntimeError, SQLAlchemyError, TenantAccessDenied) as error:
                    raise ApiError(
                        503, "cache_unavailable", "Не удалось проверить сохранённый результат распознавания. Попробуйте позже."
                    ) from error
                if claim.status == "busy":
                    raise ApiError(
                        429, "duplicate_inference_in_progress",
                        "Такая заявка уже обрабатывается в этой организации.",
                    )
                if claim.status == "hit":
                    cache_hit = True
                    try:
                        if claim.payload is None:
                            raise RuntimeError("Cached ciphertext missing.")
                        payload = decrypt_cached_payload(claim.payload)
                        extracted = ExtractedRequest.model_validate(payload)
                    except (ValidationError, RuntimeError, TypeError) as error:
                        raise ApiError(
                            503, "invalid_cached_extraction", "Сохранённый разбор заявки не прошёл проверку. Обратитесь к администратору KPDesk."
                        ) from error
                    if validate_extraction_grounding(body.text, extracted):
                        raise ApiError(
                            503, "invalid_cached_extraction", "Сохранённый разбор заявки не подтверждён исходным текстом. Обратитесь к администратору KPDesk."
                        )
            if not cache_hit:
                try:
                    budget = operation_budget(budget_factory, tenant_engine,
                        organization_id=organization_id, cookie=session_cookie, csrf=csrf,
                        key=operation_key, purpose="extraction", payload=body.text, settings=provider)
                    async with llm_operation(budget):
                        extracted = await extract_request(body.text, provider, client)
                except LLMError as error:
                    code = error.code
                    status = {
                        "rate_limited": 429,
                        "inference_quota_exceeded": 429,
                        "duplicate_inference_in_progress": 429,
                        "organization_forbidden": 403,
                        "synthetic_project_required": 503,
                        "quota_ledger_unavailable": 503,
                        "provider_timeout": 504,
                        "provider_auth_failed": 502,
                        "provider_unreachable": 502,
                        "provider_error": 502,
                        "invalid_provider_schema": 503,
                        "invalid_output": 502,
                        "incomplete_output": 502,
                    }.get(code, 502)
                    raise ApiError(
                        status, code, llm_error_message(code),
                        retry_after_seconds=error.retry_after_seconds if status == 429 else None,
                    ) from error
                except ValueError as error:
                    raise ApiError(
                        422, "invalid_request_text", "Введите текст заявки."
                    ) from error
        assert reservation is not None
        try:
            await run_thread(partial(seal_inference_preview,
                tenant_engine, request_id=reservation,
                session_cookie=session_cookie or "", source=body.text,
                extracted=extracted.model_dump(mode="json"),
            ))
        except (RuntimeError, SQLAlchemyError) as error:
            raise ApiError(
                503, "preview_audit_unavailable",
                "Не удалось сохранить результат распознавания для проверки. Проверьте состояние заявки перед повторной попыткой.",
            ) from error
        if claimed and request_hmac is not None:
            try:
                published = await run_thread(partial(publish_cached_extraction,
                    tenant_engine, organization_id=organization_id,
                    attempt_id=reservation, session_cookie=session_cookie or "",
                    csrf=csrf or "", request_hmac=request_hmac,
                    extracted=extracted.model_dump(mode="json"),
                ))
                if not published:
                    _LOG.warning("cache_publish_rejected")
            except (RuntimeError, SQLAlchemyError, TenantAccessDenied):
                # Successful sealed previews are authoritative; caching is best-effort.
                _LOG.warning("cache_publish_unavailable")
        response.headers["Cache-Control"] = "no-store"
        return TenantExtractionPreview(
            extraction_id=reservation, extracted_request=extracted, cache_hit=cache_hit,
            analysis=analyze_request(extracted),
            instruction_proposal=(
                propose_instructions("\n".join(extracted.instruction_quotes))
                if extracted.instruction_quotes else None
            ),
        )
    finally:
        if claimed and not published and request_hmac is not None and reservation is not None:
            try:
                await run_thread(partial(release_cached_extraction,
                    tenant_engine, organization_id=organization_id,
                    attempt_id=reservation, session_cookie=session_cookie or "",
                    csrf=csrf or "", request_hmac=request_hmac))
            except (RuntimeError, SQLAlchemyError, TenantAccessDenied):
                _LOG.warning("cache_pending_release_failed")

