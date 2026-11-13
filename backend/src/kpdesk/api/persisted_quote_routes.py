"""Authenticated local quote persistence, history and draft PDF transport.

No privileged database fallback and no external-send operation. Owner token is
separate from the database password, Groq key, Google credential JSON and URLs.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Callable
from dataclasses import asdict
from decimal import Decimal
from typing import Annotated, Protocol

from dotenv import dotenv_values
from fastapi import APIRouter, Depends, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from kpdesk.api.errors import ApiError, quote_conflict_message, quote_pdf_error_message
from kpdesk.config import PROJECT_ROOT
from kpdesk.db.session import app_engine, assert_unprivileged_session
from kpdesk.extraction.schema import ExtractedRequest
from kpdesk.quotes.document import CommercialDocument, DocumentApproval
from kpdesk.quotes.instructions import ConfirmedInstructions
from kpdesk.quotes.pdf import QuotePDFError, render_draft_pdf
from kpdesk.quotes.storage import (
    LockedQuoteLine,
    QuoteConflict,
    QuoteNotFound,
    StoredQuote,
    create_quote_draft,
    get_quote_draft,
    revise_quote_draft,
)


def get_owner_secret() -> str | None:
    value = dotenv_values(PROJECT_ROOT / ".env").get("KP_DESK_OWNER_TOKEN")
    return value if isinstance(value, str) and len(value) >= 40 else None


def require_owner(
    expected: Annotated[str | None, Depends(get_owner_secret)],
    supplied: Annotated[str | None, Header(alias="X-KPDesk-Token")] = None,
) -> None:
    if expected is None:
        raise ApiError(503, "owner_auth_not_configured", "Доступ владельца не настроен.")
    if supplied is None or not secrets.compare_digest(supplied, expected):
        raise ApiError(401, "owner_auth_required", "Требуется авторизация владельца.")


router = APIRouter(
    prefix="/api/quotes/drafts",
    dependencies=[Depends(require_owner)],
)


class QuoteStore(Protocol):
    def create(
        self, catalog_id: uuid.UUID, extracted: ExtractedRequest, key: str,
    ) -> StoredQuote: ...

    def revise(
        self, quote_id: uuid.UUID, expected_revision: int, catalog_id: uuid.UUID,
        extracted: ExtractedRequest, key: str,
    ) -> StoredQuote: ...

    def get(self, quote_id: uuid.UUID, revision: int | None) -> StoredQuote: ...


class PostgresQuoteStore:
    """Owns a fresh, verified session per operation, never using DB owner role."""

    def _run(self, action: Callable[[Session], StoredQuote]) -> StoredQuote:
        engine = app_engine()
        with Session(engine) as session:
            assert_unprivileged_session(session)
            session.rollback()
            return action(session)

    def create(
        self, catalog_id: uuid.UUID, extracted: ExtractedRequest, key: str,
    ) -> StoredQuote:
        return self._run(lambda session: create_quote_draft(
            session, catalog_id, extracted, key,
        ))

    def revise(
        self, quote_id: uuid.UUID, expected_revision: int, catalog_id: uuid.UUID,
        extracted: ExtractedRequest, key: str,
    ) -> StoredQuote:
        return self._run(lambda session: revise_quote_draft(
            session, quote_id, expected_revision, catalog_id, extracted, key,
        ))

    def get(self, quote_id: uuid.UUID, revision: int | None) -> StoredQuote:
        return self._run(lambda session: get_quote_draft(session, quote_id, revision))


def get_quote_store() -> QuoteStore:
    return PostgresQuoteStore()


class CreateStoredQuote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    catalog_version_id: uuid.UUID
    extracted_request: ExtractedRequest


class ReviseStoredQuote(CreateStoredQuote):
    expected_revision: int = Field(ge=1)


class StoredQuoteResponse(BaseModel):
    quote_id: uuid.UUID
    revision: int
    catalog_version_id: uuid.UUID
    ready_for_draft: bool
    total_rub: Decimal | None
    lines: tuple[LockedQuoteLine, ...]
    snapshot_sha256: str
    approved: bool
    source_request: ExtractedRequest | None = None
    instructions: ConfirmedInstructions | None = None
    base_total_rub: Decimal | None = None
    estimated_total_rub: Decimal | None = None
    budget_exceeded: bool = False
    document: CommercialDocument | None = None
    approval: DocumentApproval | None = None


def _response(quote: StoredQuote) -> StoredQuoteResponse:
    return StoredQuoteResponse.model_validate(asdict(quote))


def _required_key(raw: str | None) -> str:
    if (raw is None or not 1 <= len(raw) <= 128 or
        any(ord(c) < 33 or ord(c) > 126 for c in raw)):
        raise ApiError(422, "invalid_idempotency_key", "Не удалось определить запрос сохранения. Проверьте сохранённое КП перед повторной попыткой.")
    return raw


def _run_or_error(operation: Callable[[], StoredQuote]) -> StoredQuote:
    try:
        return operation()
    except QuoteNotFound as error:
        raise ApiError(404, "quote_not_found", "КП или его версия не найдены.") from error
    except QuoteConflict as error:
        raise ApiError(409, str(error), quote_conflict_message(str(error))) from error
    except (SQLAlchemyError, RuntimeError) as error:
        raise ApiError(503, "quote_store_unavailable", "Не удалось получить или сохранить данные КП. Проверьте список предложений перед повторной попыткой.") from error
    except ValueError as error:
        raise ApiError(422, "invalid_quote_draft", "Не удалось сохранить черновик КП. Проверьте заявку и выбранные товары.") from error


@router.post("", response_model=StoredQuoteResponse, status_code=201)
def store_quote(
    body: CreateStoredQuote,
    store: Annotated[QuoteStore, Depends(get_quote_store)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> StoredQuoteResponse:
    key = _required_key(idempotency_key)
    return _response(_run_or_error(lambda: store.create(
        body.catalog_version_id, body.extracted_request, key,
    )))


@router.post("/{quote_id}/revisions", response_model=StoredQuoteResponse, status_code=201)
def update_quote(
    quote_id: uuid.UUID, body: ReviseStoredQuote,
    store: Annotated[QuoteStore, Depends(get_quote_store)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> StoredQuoteResponse:
    key = _required_key(idempotency_key)
    return _response(_run_or_error(lambda: store.revise(
        quote_id, body.expected_revision, body.catalog_version_id,
        body.extracted_request, key,
    )))


@router.get("/{quote_id}", response_model=StoredQuoteResponse)
def read_quote(
    quote_id: uuid.UUID, store: Annotated[QuoteStore, Depends(get_quote_store)],
    revision: Annotated[int | None, Query(ge=1)] = None,
) -> StoredQuoteResponse:
    return _response(_run_or_error(lambda: store.get(quote_id, revision)))


@router.get("/{quote_id}/pdf")
def quote_pdf(
    quote_id: uuid.UUID, store: Annotated[QuoteStore, Depends(get_quote_store)],
    revision: Annotated[int | None, Query(ge=1)] = None,
) -> Response:
    quote = _run_or_error(lambda: store.get(quote_id, revision))
    try:
        content = render_draft_pdf(quote)
    except QuotePDFError as error:
        status = 409 if str(error) in ("incomplete_draft", "inconsistent_quote_total") else 503
        raise ApiError(status, str(error), quote_pdf_error_message(str(error))) from error
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": (
                f'attachment; filename="KPDesk-Draft-{quote.quote_id}-r{quote.revision}.pdf"'
            ),
        },
    )
