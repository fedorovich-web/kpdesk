"""Authenticated cross-supplier comparison and explicit, price-locked quote creation.

Requires tenant-scoped reviewed profile identity and immutable source proof.
Never trusts an offer, price, SKU, or source provenance supplied by a client.
No external LLM or automatic supplier selection.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import asdict
from typing import Annotated, Literal

from fastapi import APIRouter, Cookie, Depends, Header, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from kpdesk.api.errors import ApiError
from kpdesk.api.persisted_quote_routes import StoredQuoteResponse
from kpdesk.api.tenant_quote_routes import _handle_error, _required_key, _store
from kpdesk.api.tenant_session_routes import (
    SESSION_COOKIE,
    get_unreleased_auth_repository,
)
from kpdesk.catalog.supplier_catalog_diff import SupplierCatalogDiff, supplier_catalog_diff
from kpdesk.catalog.supplier_source_discovery import (
    SupplierSourceDiscovery,
    discover_supplier_sources,
)
from kpdesk.catalog.tenant_storage import TenantCatalogRepository
from kpdesk.catalog.verified_supplier_sources import SupplierSourceRef
from kpdesk.db.auth_sessions import AuthSessionRepository
from kpdesk.db.tenant_access import TenantAccessDenied
from kpdesk.extraction.schema import ExtractedRequest
from kpdesk.quotes.instructions import ConfirmedInstructions
from kpdesk.quotes.supplier_comparison import SupplierOfferSelection

router = APIRouter(
    prefix="/api/session/organizations/{organization_id}/quotes/supplier-comparison"
)


class SupplierSourceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supplier_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,120}$")
    profile_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_version_id: uuid.UUID

    def source_ref(self) -> SupplierSourceRef:
        return SupplierSourceRef(
            self.supplier_id, self.profile_fingerprint, self.catalog_version_id,
        )


class SupplierCompareBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: tuple[SupplierSourceBody, ...] = Field(min_length=2, max_length=10)
    extracted_request: ExtractedRequest
    instructions: ConfirmedInstructions | None = None


class SupplierComposeBody(SupplierCompareBody):
    active_catalog_version_id: uuid.UUID
    selections: tuple[SupplierOfferSelection, ...] = Field(default=(), max_length=20)


class SupplierPreviewOffer(BaseModel):
    supplier_id: str
    catalog_version_id: uuid.UUID
    source_sha256: str
    sku: str
    product_name: str
    product_brand: str | None
    sale_quantity: int
    sale_unit: str
    unit_price_rub: str
    base_total_rub: str
    selling_total_rub: str


class SupplierPreviewLine(BaseModel):
    line_number: int
    description: str
    status: Literal["select_supplier", "no_verified_offer", "request_needs_review"]
    offers: list[SupplierPreviewOffer]


class SupplierPreviewResponse(BaseModel):
    requires_manager_confirmation: Literal[True] = True
    catalog_source_count: int
    lines: list[SupplierPreviewLine]


class SupplierDiscoverBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supplier_ids: tuple[str, ...] = Field(min_length=2, max_length=10)


class SupplierDiscoveredOption(SupplierSourceBody):
    source_sha256: str
    rows_count: int
    evidence: Literal["original", "reviewed_canonical"]


class SupplierDiscoveredGroup(BaseModel):
    supplier_id: str
    options: list[SupplierDiscoveredOption]
    truncated: bool
    canonical_discovery_available: bool


class SupplierDiscoveryResponse(BaseModel):
    suppliers: list[SupplierDiscoveredGroup]


class SingleSupplierDiscoveryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supplier_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,120}$")


def _discovery_group(group: SupplierSourceDiscovery) -> SupplierDiscoveredGroup:
    return SupplierDiscoveredGroup(
        supplier_id=group.supplier_id, truncated=group.truncated,
        canonical_discovery_available=group.canonical_discovery_available,
        options=[
            SupplierDiscoveredOption(
                supplier_id=source.supplier_id,
                profile_fingerprint=source.profile_fingerprint,
                catalog_version_id=source.catalog_version_id,
                source_sha256=source.source_sha256,
                rows_count=source.rows_count,
                evidence=source.evidence,
            )
            for source in group.sources
        ],
    )


class SupplierCatalogDiffBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    before: SupplierSourceBody
    after: SupplierSourceBody
    offset: int = Field(default=0, ge=0, le=10000)
    limit: int = Field(default=50, ge=1, le=100)


class SupplierCatalogChangeResponse(BaseModel):
    sku: str
    kind: Literal[
        "added", "removed", "price_changed", "product_changed",
        "product_and_price_changed",
    ]
    changed_fields: list[str]
    before_price_rub: str | None
    after_price_rub: str | None
    comparable_price_delta_rub: str | None


class SupplierCatalogDiffResponse(BaseModel):
    supplier_id: str
    before_catalog_version_id: uuid.UUID
    after_catalog_version_id: uuid.UUID
    before_source_sha256: str
    after_source_sha256: str
    counts: dict[str, int]
    total_changes: int
    offset: int
    next_offset: int | None
    requires_manager_review: Literal[True]
    changes: list[SupplierCatalogChangeResponse]


@router.post("/supplier-sources", response_model=SupplierDiscoveredGroup)
def discover_single_supplier_versions(
    organization_id: uuid.UUID,
    body: SingleSupplierDiscoveryBody,
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> SupplierDiscoveredGroup:
    store = _store(repository, session_cookie, organization_id, write=True, csrf=csrf)
    catalog = TenantCatalogRepository(
        repository.connection, store.identity, organization_id,
        session_token=session_cookie or "",
    )
    try:
        group = discover_supplier_sources(catalog, body.supplier_id)
    except (TenantAccessDenied, ValueError, TypeError, RuntimeError, SQLAlchemyError) as error:
        _handle_error(error)
    response.headers["Cache-Control"] = "no-store"
    return _discovery_group(group)


@router.post("/source-diff", response_model=SupplierCatalogDiffResponse)
def compare_supplier_source_versions(
    organization_id: uuid.UUID,
    body: SupplierCatalogDiffBody,
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> SupplierCatalogDiffResponse:
    store = _store(repository, session_cookie, organization_id, write=True, csrf=csrf)
    catalog = TenantCatalogRepository(
        repository.connection, store.identity, organization_id,
        session_token=session_cookie or "",
    )
    try:
        result = supplier_catalog_diff(
            catalog, body.before.source_ref(), body.after.source_ref(),
            offset=body.offset, limit=body.limit,
        )
    except (TenantAccessDenied, ValueError, TypeError, RuntimeError, SQLAlchemyError) as error:
        _handle_error(error)
    response.headers["Cache-Control"] = "no-store"
    return supplier_diff_response(result)


def supplier_diff_response(result: SupplierCatalogDiff) -> SupplierCatalogDiffResponse:
    """One stable HTTP contract for active and historical read-only reports."""
    return SupplierCatalogDiffResponse(
        supplier_id=result.supplier_id,
        before_catalog_version_id=result.before_catalog_version_id,
        after_catalog_version_id=result.after_catalog_version_id,
        before_source_sha256=result.before_source_sha256,
        after_source_sha256=result.after_source_sha256,
        counts=result.counts, total_changes=result.total_changes,
        offset=result.offset, next_offset=result.next_offset,
        requires_manager_review=True,
        changes=[
            SupplierCatalogChangeResponse(
                sku=item.sku, kind=item.kind,
                changed_fields=list(item.changed_fields),
                before_price_rub=(
                    str(item.before_price_rub) if item.before_price_rub is not None else None
                ),
                after_price_rub=(
                    str(item.after_price_rub) if item.after_price_rub is not None else None
                ),
                comparable_price_delta_rub=(
                    str(item.comparable_price_delta_rub)
                    if item.comparable_price_delta_rub is not None else None
                ),
            )
            for item in result.changes
        ],
    )


@router.post("/sources", response_model=SupplierDiscoveryResponse)
def discover_supplier_quote_sources(
    organization_id: uuid.UUID,
    body: SupplierDiscoverBody,
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> SupplierDiscoveryResponse:
    if (
        len(set(body.supplier_ids)) != len(body.supplier_ids)
        or any(
            re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", supplier_id) is None
            for supplier_id in body.supplier_ids
        )
    ):
        raise ApiError(422, "invalid_suppliers", "Выберите разных поставщиков из доступного списка. Проверьте выбор и повторите сравнение.")
    store = _store(repository, session_cookie, organization_id, write=True, csrf=csrf)
    catalog = TenantCatalogRepository(
        repository.connection, store.identity, organization_id,
        session_token=session_cookie or "",
    )
    try:
        discovered = [
            discover_supplier_sources(catalog, supplier_id)
            for supplier_id in body.supplier_ids
        ]
    except (TenantAccessDenied, ValueError, TypeError, RuntimeError, SQLAlchemyError) as error:
        _handle_error(error)
    response.headers["Cache-Control"] = "no-store"
    return SupplierDiscoveryResponse(suppliers=[_discovery_group(group) for group in discovered])


@router.post("/preview", response_model=SupplierPreviewResponse)
def preview_supplier_comparison(
    organization_id: uuid.UUID,
    body: SupplierCompareBody,
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> SupplierPreviewResponse:
    store = _store(repository, session_cookie, organization_id, write=True, csrf=csrf)
    try:
        result = store.compare_suppliers(
            tuple(source.source_ref() for source in body.sources),
            body.extracted_request, body.instructions,
        )
    except (TenantAccessDenied, ValueError, TypeError, RuntimeError, SQLAlchemyError) as error:
        _handle_error(error)
    response.headers["Cache-Control"] = "no-store"
    return SupplierPreviewResponse(
        catalog_source_count=result.catalog_source_count,
        lines=[
            SupplierPreviewLine(
                line_number=line.line_number, description=line.description,
                status=line.status,
                offers=[
                    SupplierPreviewOffer(
                        supplier_id=item.supplier_id,
                        catalog_version_id=item.catalog_version_id,
                        source_sha256=item.source_sha256,
                        sku=item.product.sku, product_name=item.product.name,
                        product_brand=item.product.brand,
                        sale_quantity=item.sale_quantity, sale_unit=item.sale_unit,
                        unit_price_rub=str(item.unit_price_rub),
                        base_total_rub=str(item.base_total_rub),
                        selling_total_rub=str(item.selling_total_rub),
                    ) for item in line.offers
                ],
            ) for line in result.lines
        ],
    )


@router.post("", status_code=201, response_model=StoredQuoteResponse)
def create_supplier_quote(
    organization_id: uuid.UUID,
    body: SupplierComposeBody,
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> StoredQuoteResponse:
    key = _required_key(idempotency_key)
    store = _store(repository, session_cookie, organization_id, write=True, csrf=csrf)
    try:
        quote = store.create_supplier_quote(
            active_catalog_id=body.active_catalog_version_id,
            source_refs=tuple(source.source_ref() for source in body.sources),
            extracted=body.extracted_request,
            selections=body.selections,
            instructions=body.instructions,
            key=key,
        )
    except (TenantAccessDenied, ValueError, TypeError, RuntimeError, SQLAlchemyError) as error:
        _handle_error(error)
    response.headers["Cache-Control"] = "no-store"
    return StoredQuoteResponse.model_validate(asdict(quote))
