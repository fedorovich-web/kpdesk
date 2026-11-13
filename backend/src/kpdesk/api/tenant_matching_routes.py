from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Annotated, Literal

from fastapi import APIRouter, Cookie, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from kpdesk.api.errors import ApiError
from kpdesk.api.routes import (
    CategorySuggestionBody,
    CategorySuggestionResponse,
    _enabled_models,
)
from kpdesk.api.tenant_session_routes import (
    SESSION_COOKIE,
    _required_session,
    get_unreleased_auth_repository,
)
from kpdesk.catalog.tenant_storage import TenantCatalogRepository
from kpdesk.db.auth_sessions import AuthSessionRepository
from kpdesk.db.tenant_access import TenantAccessDenied
from kpdesk.extraction.analysis import RequirementCheck, product_requirements
from kpdesk.matching.artifact import TrustedModel
from kpdesk.matching.category_classifier import suggest_category_text
from kpdesk.matching.rank import rank_candidates
from kpdesk.matching.retrieve import retrieve_candidates
from kpdesk.matching.schema import Candidate, CatalogProduct, RequestedItem
from kpdesk.quotes.instructions import (
    ConfirmedInstructions,
    QuoteRules,
    check_confirmation,
    filter_catalog_by_brand,
)
from kpdesk.quotes.variants import CompositionVariant, build_variants

router = APIRouter(prefix="/api/session/organizations/{organization_id}/matches")


def _catalog(
    repository: AuthSessionRepository,
    organization_id: uuid.UUID,
    cookie: str | None,
    csrf: str | None,
) -> TenantCatalogRepository:
    identity = _required_session(repository, cookie, csrf=csrf, write=True)
    catalog = TenantCatalogRepository(
        repository.connection, identity, organization_id,
        session_token=cookie, csrf_token=csrf,
    )
    try:
        catalog._authorize("write")
    except TenantAccessDenied as error:
        raise ApiError(403, "organization_forbidden", "У вас нет доступа к подбору товаров в этой организации.") from error
    return catalog


@router.post("/category-suggestion", response_model=CategorySuggestionResponse)
def tenant_category_suggestion(
    organization_id: uuid.UUID,
    body: CategorySuggestionBody,
    request: Request,
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> CategorySuggestionResponse:
    catalog = _catalog(repository, organization_id, cookie, csrf)
    try:
        catalog.current()
    except TenantAccessDenied as error:
        raise ApiError(403, "organization_forbidden", "У вас нет доступа к подбору товаров в этой организации.") from error
    except SQLAlchemyError as error:
        raise ApiError(503, "catalog_unavailable", "Не удалось загрузить прайс для подбора товаров. Попробуйте позже.") from error
    models = _enabled_models(request)
    suggestion = suggest_category_text(models.classifier, body.description)
    response.headers["Cache-Control"] = "no-store"
    return CategorySuggestionResponse(
        original_category=body.original_category,
        suggested_category=suggestion.predicted,
        best_category=suggestion.best_category,
        confidence=suggestion.confidence,
        margin=suggestion.margin,
        abstained=suggestion.abstained,
    )


class TenantMatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    catalog_version_id: uuid.UUID
    requested_item: RequestedItem
    limit: int = Field(default=20, ge=1, le=20)
    mode: Literal["ml", "rapidfuzz", "tfidf"] = "ml"
    instructions: ConfirmedInstructions | None = None


class TenantMatchCandidate(BaseModel):
    sku: str
    name: str
    price_rub: str
    sale_unit: Literal["штука", "пачка", "коробка"]
    pieces_per_sale_unit: int
    score: float = Field(ge=0, le=1)
    status: Literal["compatible", "needs_review"]
    reason_codes: tuple[str, ...]
    requirements: tuple[RequirementCheck, ...] = ()


class TenantMatchResponse(BaseModel):
    catalog_version_id: uuid.UUID
    mode: Literal["ml", "rapidfuzz", "tfidf"]
    requires_manager_confirmation: bool = True
    calibrated_no_match: bool = False
    candidates: list[TenantMatchCandidate]


class TenantBatchMatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    catalog_version_id: uuid.UUID
    requested_items: list[RequestedItem] = Field(min_length=1, max_length=20)
    limit: int = Field(default=20, ge=1, le=20)
    mode: Literal["ml", "rapidfuzz", "tfidf"] = "tfidf"
    instructions: ConfirmedInstructions | None = None


class TenantBatchMatchLine(TenantMatchResponse):
    line_number: int = Field(ge=1, le=20)


class TenantBatchMatchResponse(BaseModel):
    catalog_version_id: uuid.UUID
    mode: Literal["ml", "rapidfuzz", "tfidf"]
    lines: list[TenantBatchMatchLine]


class TenantVariantsResponse(TenantBatchMatchResponse):
    candidate_limit_per_line: int
    variants: list[CompositionVariant]


def _matching_products(
    catalog: TenantCatalogRepository,
    version: uuid.UUID,
    instructions: ConfirmedInstructions | None,
) -> list[CatalogProduct]:
    try:
        rules = check_confirmation(instructions) if instructions else None
    except ValueError as error:
        raise ApiError(422, "invalid_instructions", "Правила подбора изменились после подтверждения. Проверьте и подтвердите их заново.") from error
    try:
        current = catalog.current()
        if current is None:
            raise ApiError(404, "no_active_catalog", "Активный прайс пока не загружен. Загрузите прайс перед подбором товаров.")
        if current.id != version:
            raise ApiError(409, "stale_catalog", "Активный прайс изменился. Обновите подбор и проверьте цены.")
        products = catalog.items(current.id)
    except TenantAccessDenied as error:
        raise ApiError(403, "organization_forbidden", "У вас нет доступа к подбору товаров в этой организации.") from error
    except SQLAlchemyError as error:
        raise ApiError(503, "catalog_unavailable", "Не удалось загрузить прайс для подбора товаров. Попробуйте позже.") from error
    return filter_catalog_by_brand(products, rules)


def _preview(
    version: uuid.UUID, item: RequestedItem, products: Sequence[CatalogProduct],
    limit: int, mode: Literal["ml", "rapidfuzz", "tfidf"], artifact: TrustedModel | None,
) -> TenantMatchResponse:
    retrieved = retrieve_candidates(item, products, limit=limit)
    ranked = rank_candidates(item, retrieved, mode, artifact)
    return _match_response(version, item, ranked, mode)


def _match_response(
    version: uuid.UUID, item: RequestedItem, ranked: Sequence[Candidate],
    mode: Literal["ml", "rapidfuzz", "tfidf"],
) -> TenantMatchResponse:
    return TenantMatchResponse(
        catalog_version_id=version, mode=mode,
        candidates=[
            TenantMatchCandidate(
                sku=candidate.product.sku, name=candidate.product.name,
                price_rub=str(candidate.product.price_rub), sale_unit=candidate.product.sale_unit,
                pieces_per_sale_unit=candidate.product.pieces_per_sale_unit,
                score=candidate.score, status=candidate.constraints.status,
                reason_codes=candidate.constraints.reason_codes,
                requirements=product_requirements(item, candidate.product),
            ) for candidate in ranked if candidate.constraints.status != "incompatible"
        ],
    )


@router.post("/preview", response_model=TenantMatchResponse)
def tenant_match_preview(
    organization_id: uuid.UUID,
    body: TenantMatchRequest,
    request: Request,
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> TenantMatchResponse:
    catalog = _catalog(repository, organization_id, cookie, csrf)
    products = _matching_products(catalog, body.catalog_version_id, body.instructions)
    artifact = _enabled_models(request).reranker if body.mode == "ml" else None
    response.headers["Cache-Control"] = "no-store"
    return _preview(
        body.catalog_version_id, body.requested_item, products, body.limit, body.mode, artifact,
    )


@router.post("/batch-preview", response_model=TenantBatchMatchResponse)
def tenant_batch_match_preview(
    organization_id: uuid.UUID,
    body: TenantBatchMatchRequest,
    request: Request,
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> TenantBatchMatchResponse:
    catalog = _catalog(repository, organization_id, cookie, csrf)
    products = _matching_products(catalog, body.catalog_version_id, body.instructions)
    artifact = _enabled_models(request).reranker if body.mode == "ml" else None
    response.headers["Cache-Control"] = "no-store"
    return TenantBatchMatchResponse(
        catalog_version_id=body.catalog_version_id, mode=body.mode,
        lines=[TenantBatchMatchLine(
            line_number=index,
            **_preview(
                body.catalog_version_id, item, products, body.limit, body.mode, artifact,
            ).model_dump(),
        ) for index, item in enumerate(body.requested_items, 1)],
    )


@router.post("/variants", response_model=TenantVariantsResponse)
def tenant_composition_variants(
    organization_id: uuid.UUID,
    body: TenantBatchMatchRequest,
    request: Request,
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> TenantVariantsResponse:
    catalog = _catalog(repository, organization_id, cookie, csrf)
    products = _matching_products(catalog, body.catalog_version_id, body.instructions)
    artifact = _enabled_models(request).reranker if body.mode == "ml" else None
    pools = [rank_candidates(
        item, retrieve_candidates(item, products, limit=body.limit), body.mode, artifact,
    ) for item in body.requested_items]
    response.headers["Cache-Control"] = "no-store"
    return TenantVariantsResponse(
        catalog_version_id=body.catalog_version_id, mode=body.mode,
        candidate_limit_per_line=body.limit,
        lines=[TenantBatchMatchLine(line_number=index, **_match_response(
            body.catalog_version_id, item, pool, body.mode,
        ).model_dump()) for index, (item, pool) in enumerate(
            zip(body.requested_items, pools, strict=True), 1,
        )],
        variants=build_variants(body.requested_items, pools,
                                check_confirmation(body.instructions)
                                if body.instructions else QuoteRules()),
    )
