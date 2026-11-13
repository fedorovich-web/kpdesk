"""Catalog HTTP surface; external import is explicitly confirmed and validated."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from typing import Annotated, Literal, Protocol

import httpx
from fastapi import APIRouter, Depends, File, Header, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from kpdesk.api.errors import ApiError
from kpdesk.catalog.parse import parse_price_list
from kpdesk.catalog.schema import CatalogImportResult, ImportLimits
from kpdesk.catalog.storage import (
    CatalogImportConflict,
    CatalogStateError,
    PublishedCatalog,
    get_active_catalog,
    list_catalog_items,
    publish_catalog,
)
from kpdesk.db.session import app_engine, assert_unprivileged_session
from kpdesk.extraction.schema import ExtractedLine, ExtractedRequest
from kpdesk.extraction.to_matching import ExtractionNeedsReview, to_requested_item
from kpdesk.llm.settings import LLMSettings
from kpdesk.matching.category_classifier import suggest_category_text
from kpdesk.matching.rank import rank_candidates
from kpdesk.matching.retrieve import retrieve_candidates
from kpdesk.matching.runtime import LocalMLModels, load_local_ml_models
from kpdesk.matching.schema import CatalogProduct, RequestedItem

router = APIRouter(prefix="/api")


class CatalogRepository(Protocol):
    def publish(
        self,
        source: CatalogImportResult,
        idempotency_key: str,
        request_hash: str,
    ) -> PublishedCatalog: ...

    def current(self) -> PublishedCatalog | None: ...

    def items(self, version_id: uuid.UUID) -> list[CatalogProduct]: ...


class PostgresCatalogRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def publish(
        self,
        source: CatalogImportResult,
        idempotency_key: str,
        request_hash: str,
    ) -> PublishedCatalog:
        return publish_catalog(self.session, source, idempotency_key, request_hash)

    def current(self) -> PublishedCatalog | None:
        current = get_active_catalog(self.session)
        if current is None:
            return None
        return PublishedCatalog(
            id=current.id,
            source_sha256=current.source_sha256,
            rows_count=current.rows_count,
        )

    def items(self, version_id: uuid.UUID) -> list[CatalogProduct]:
        return list_catalog_items(self.session, version_id)


def get_catalog_repository() -> Iterator[CatalogRepository]:
    """Fail closed until an unprivileged app role exists. Tests override this dependency."""
    try:
        engine = app_engine()
        with Session(engine) as session:
            assert_unprivileged_session(session)
            session.rollback()  # the privilege check was a read transaction
            yield PostgresCatalogRepository(session)
    except (SQLAlchemyError, RuntimeError) as error:
        raise ApiError(
            503, "database_unavailable", "Локальная база приложения недоступна."
        ) from error


class CatalogVersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_sha256: str
    rows_count: int


class CatalogItemsResponse(BaseModel):
    items: list[CatalogProduct]


@router.get("/health/live")
def health_live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
def health_ready(
    repository: Annotated[CatalogRepository, Depends(get_catalog_repository)],
) -> dict[str, str]:
    repository.current()
    return {"status": "ready"}


@router.get("/capabilities")
def capabilities(request: Request) -> dict[str, bool]:
    configured = False  # Legacy unauthenticated app has no shared tenant budget.
    return {
        "llm_available": configured,
        "model_trained": _models_available(request),
        "bitrix24_connected": False,
        "one_c_connected": False,
        "amocrm_connected": False,
        "google_workspace_connected": False,
    }


@router.get("/catalog/current", response_model=CatalogVersionResponse)
def catalog_current(
    repository: Annotated[CatalogRepository, Depends(get_catalog_repository)],
) -> PublishedCatalog:
    published = repository.current()
    if published is None:
        raise ApiError(404, "no_active_catalog", "Прайс ещё не импортирован.")
    return published


@router.get("/catalog/items", response_model=CatalogItemsResponse)
def catalog_items(
    repository: Annotated[CatalogRepository, Depends(get_catalog_repository)],
) -> CatalogItemsResponse:
    published = repository.current()
    if published is None:
        return CatalogItemsResponse(items=[])
    return CatalogItemsResponse(items=repository.items(published.id))


@router.post(
    "/catalog/import",
    status_code=201,
    response_model=CatalogVersionResponse,
)
def catalog_import(
    request: Request,
    repository: Annotated[CatalogRepository, Depends(get_catalog_repository)],
    file: Annotated[UploadFile, File()],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> PublishedCatalog:
    if not idempotency_key or not 1 <= len(idempotency_key) <= 128:
        raise ApiError(
            422, "missing_idempotency_key", "Не удалось определить запрос загрузки прайса. Проверьте активную версию перед повторной попыткой."
        )
    filename = file.filename or ""
    if (
        not filename or len(filename) > 250
        or "/" in filename or "\\" in filename
        or any(ord(char) < 32 for char in filename)
    ):
        raise ApiError(422, "invalid_filename", "Недопустимое имя файла.")
    maximum = request.app.state.settings.max_upload_bytes
    payload = file.file.read(maximum + 1)
    if len(payload) > maximum:
        raise ApiError(413, "file_too_large", "Размер файла прайса превышает лимит загрузки.")

    result = parse_price_list(filename, payload, ImportLimits(max_upload_bytes=maximum))
    if not result.publishable:
        detail = result.errors[0]
        raise ApiError(
            422, "invalid_catalog", detail.message, row=detail.row, field=detail.field
        )
    try:
        return repository.publish(result, idempotency_key, result.source_sha256)
    except CatalogImportConflict as error:
        raise ApiError(
            409, "idempotency_conflict", "Не удалось подтвердить повторную загрузку прайса. Проверьте активную версию перед новой попыткой."
        ) from error
    except CatalogStateError as error:
        raise ApiError(503, "catalog_state_error", "Хранилище каталога недоступно.") from error



def get_llm_settings() -> LLMSettings | None:
    """Local key only; HTTP clients never receive it from the frontend."""
    try:
        return LLMSettings.from_local_env()
    except ValueError:
        return None


async def get_llm_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(20.0, connect=5.0),
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
    ) as client:
        yield client


# Function identity remains compatible with existing dependency overrides.
get_groq_settings = get_llm_settings
get_groq_client = get_llm_client

type GroqModel = Literal["nvidia/nemotron-3-ultra-550b-a55b:free"]


class ExtractRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=12_000)
    model: GroqModel | None = None


@router.post("/requests/extract", response_model=ExtractedRequest)
async def api_extract_request(
    body: ExtractRequestBody,
    provider: Annotated[LLMSettings | None, Depends(get_groq_settings)],
    client: Annotated[httpx.AsyncClient, Depends(get_groq_client)],
) -> ExtractedRequest:
    if provider is None:
        raise ApiError(503, "provider_not_configured", "Ключ OpenRouter не настроен.")
    raise ApiError(503, "authenticated_provider_required",
                   "Для ИИ войдите в тестовый кабинет организации с общим учётом запросов.")



def _models_available(request: Request) -> bool:
    if not request.app.state.settings.enable_synthetic_ml:
        return False
    try:
        load_local_ml_models()
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError):
        return False
    return True


def _enabled_models(request: Request) -> LocalMLModels:
    if not request.app.state.settings.enable_synthetic_ml:
        raise ApiError(503, "model_unavailable", "ML-подбор не включён для этого окружения.")
    try:
        return load_local_ml_models()
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError) as error:
        raise ApiError(503, "model_unavailable", "Проверенная ML-модель недоступна.") from error


class CategorySuggestionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=2000)
    original_category: str | None = None


class CategorySuggestionResponse(BaseModel):
    original_category: str | None
    suggested_category: str | None
    best_category: str
    confidence: float = Field(ge=0, le=1)
    margin: float = Field(ge=0, le=1)
    abstained: bool
    requires_explicit_acceptance: bool = True


@router.post("/matches/category-suggestion", response_model=CategorySuggestionResponse)
def api_category_suggestion(
    body: CategorySuggestionBody,
    request: Request,
) -> CategorySuggestionResponse:
    models = _enabled_models(request)
    suggestion = suggest_category_text(models.classifier, body.description)
    return CategorySuggestionResponse(
        original_category=body.original_category,
        suggested_category=suggestion.predicted,
        best_category=suggestion.best_category,
        confidence=suggestion.confidence,
        margin=suggestion.margin,
        abstained=suggestion.abstained,
    )


class MatchPreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requested_item: RequestedItem
    mode: Literal["rapidfuzz", "tfidf", "ml"] = "rapidfuzz"
    limit: int = Field(default=20, ge=1, le=20)


class MatchedCandidateResponse(BaseModel):
    sku: str
    name: str
    price_rub: Decimal
    score: float = Field(ge=0, le=1)
    status: Literal["compatible", "needs_review"]
    reason_codes: tuple[str, ...]
    requires_manager_confirmation: bool = True


class MatchPreviewResponse(BaseModel):
    catalog_version_id: uuid.UUID
    mode: Literal["rapidfuzz", "tfidf", "ml"]
    requires_manager_confirmation: bool = True
    candidates: list[MatchedCandidateResponse]


@router.post("/matches/preview", response_model=MatchPreviewResponse)
def api_match_preview(
    body: MatchPreviewBody,
    request: Request,
    repository: Annotated[CatalogRepository, Depends(get_catalog_repository)],
) -> MatchPreviewResponse:
    published = repository.current()
    if published is None:
        raise ApiError(404, "no_active_catalog", "Для подбора сначала импортируйте прайс.")
    models = _enabled_models(request) if body.mode == "ml" else None
    products = repository.items(published.id)
    retrieved = retrieve_candidates(body.requested_item, products, limit=body.limit)
    ranked = rank_candidates(
        body.requested_item, retrieved, mode=body.mode,
        artifact=models.reranker if models is not None else None,
    )
    return MatchPreviewResponse(
        catalog_version_id=published.id,
        mode=body.mode,
        candidates=[
            MatchedCandidateResponse(
                sku=candidate.product.sku,
                name=candidate.product.name,
                price_rub=candidate.product.price_rub,
                score=candidate.score,
                status=candidate.constraints.status,  # incompatible removed earlier
                reason_codes=candidate.constraints.reason_codes,
            )
            for candidate in ranked
            if candidate.constraints.status != "incompatible"
        ],
    )



class ExtractedMatchPreviewBody(BaseModel):
    """Validated output from OpenRouter or manually edited before matching."""
    model_config = ConfigDict(extra="forbid")

    extracted_line: ExtractedLine
    mode: Literal["rapidfuzz", "tfidf", "ml"] = "rapidfuzz"
    limit: int = Field(default=20, ge=1, le=20)


@router.post("/matches/preview-extracted", response_model=MatchPreviewResponse)
def api_match_extracted(
    body: ExtractedMatchPreviewBody,
    http_request: Request,
    repository: Annotated[CatalogRepository, Depends(get_catalog_repository)],
) -> MatchPreviewResponse:
    try:
        request = to_requested_item(body.extracted_line)
    except ExtractionNeedsReview as error:
        raise ApiError(
            422, "category_needs_review", "Уточните категорию позиции вручную."
        ) from error
    return api_match_preview(
        MatchPreviewBody(requested_item=request, mode=body.mode, limit=body.limit),
        http_request,
        repository,
    )
