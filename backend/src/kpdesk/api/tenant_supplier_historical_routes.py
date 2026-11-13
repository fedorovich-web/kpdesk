"""A102 supplier price-history audit; intentionally inaccessible to quote creation.

POSTs are authenticated/CSRF protected because only verified tenant sessions
may retrieve source-price history. No capability to publish or select for quotes.
"""
from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Cookie, Depends, Header, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from kpdesk.api.tenant_quote_routes import _handle_error, _store
from kpdesk.api.tenant_session_routes import (
    SESSION_COOKIE,
    get_unreleased_auth_repository,
)
from kpdesk.api.tenant_supplier_comparison_routes import (
    SupplierCatalogDiffResponse,
    supplier_diff_response,
)
from kpdesk.catalog.supplier_historical_sources import (
    HistoricalSupplierRef,
    compare_historical_supplier_sources,
    discover_historical_supplier_sources,
)
from kpdesk.catalog.tenant_storage import TenantCatalogRepository
from kpdesk.db.auth_sessions import AuthSessionRepository
from kpdesk.db.tenant_access import TenantAccessDenied

router = APIRouter(
    prefix="/api/session/organizations/{organization_id}/supplier-price-history"
)


class HistoricalSupplierRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supplier_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,120}$")


class HistoricalSourceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supplier_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,120}$")
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_version_id: uuid.UUID

    def ref(self) -> HistoricalSupplierRef:
        return HistoricalSupplierRef(
            self.supplier_id, self.profile_sha256, self.catalog_version_id,
        )


class HistoricalOption(HistoricalSourceModel):
    profile_fingerprint: str
    source_sha256: str
    rows_count: int
    evidence: Literal["original"]


class HistoricalSourceListing(BaseModel):
    supplier_id: str
    options: list[HistoricalOption]
    truncated: bool
    audit_only: Literal[True]


class HistoricalDiffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    before: HistoricalSourceModel
    after: HistoricalSourceModel
    offset: int = Field(default=0, ge=0, le=10000)
    limit: int = Field(default=50, ge=1, le=100)


def _catalog(
    organization_id: uuid.UUID,
    repository: AuthSessionRepository,
    session_cookie: str | None,
    csrf: str | None,
) -> TenantCatalogRepository:
    store = _store(repository, session_cookie, organization_id, write=True, csrf=csrf)
    return TenantCatalogRepository(
        repository.connection, store.identity, organization_id,
        session_token=session_cookie or "",
    )


@router.post("/sources", response_model=HistoricalSourceListing)
def historical_sources(
    organization_id: uuid.UUID,
    body: HistoricalSupplierRequest,
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> HistoricalSourceListing:
    catalog = _catalog(organization_id, repository, session_cookie, csrf)
    try:
        listing = discover_historical_supplier_sources(catalog, body.supplier_id)
    except (TenantAccessDenied, ValueError, TypeError, RuntimeError, SQLAlchemyError) as error:
        _handle_error(error)
    response.headers["Cache-Control"] = "no-store"
    return HistoricalSourceListing(
        supplier_id=listing.supplier_id,
        truncated=listing.truncated,
        audit_only=True,
        options=[
            HistoricalOption(
                supplier_id=item.supplier_id,
                profile_sha256=item.profile_sha256,
                profile_fingerprint=item.profile_fingerprint,
                catalog_version_id=item.catalog_version_id,
                source_sha256=item.source_sha256,
                rows_count=item.rows_count, evidence=item.evidence,
            )
            for item in listing.options
        ],
    )


@router.post("/diff", response_model=SupplierCatalogDiffResponse)
def historical_diff(
    organization_id: uuid.UUID,
    body: HistoricalDiffRequest,
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> SupplierCatalogDiffResponse:
    catalog = _catalog(organization_id, repository, session_cookie, csrf)
    try:
        result = compare_historical_supplier_sources(
            catalog, body.before.ref(), body.after.ref(),
            offset=body.offset, limit=body.limit,
        )
    except (TenantAccessDenied, ValueError, TypeError, RuntimeError, SQLAlchemyError) as error:
        _handle_error(error)
    response.headers["Cache-Control"] = "no-store"
    return supplier_diff_response(result)
