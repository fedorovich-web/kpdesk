"""Session/editor-authorized read-only lookup, outside the DB transaction."""
from __future__ import annotations

import uuid
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Header, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from kpdesk.api.errors import ApiError
from kpdesk.api.tenant_session_routes import (
    SESSION_COOKIE,
    _required_session,
    get_unreleased_catalog_engine,
)
from kpdesk.catalog.tenant_storage import TenantCatalogRepository
from kpdesk.company.lookup import (
    CompanyLookupError,
    CompanyLookupResult,
    CompanyLookupService,
    LookupSettings,
    validate_inn,
)
from kpdesk.db.auth_sessions import AuthSessionRepository
from kpdesk.db.tenant_access import TenantAccessDenied

router = APIRouter(prefix="/api/session/organizations/{organization_id}")


class CompanyLookupBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    inn: str = Field(min_length=10, max_length=12)

    @field_validator("inn")
    @classmethod
    def valid_inn(cls, value: str) -> str:
        return validate_inn(value)


def authorize_company_lookup(
    organization_id: uuid.UUID,
    engine: Annotated[Engine, Depends(get_unreleased_catalog_engine)],
    cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> uuid.UUID:
    """Revalidate current session, CSRF and membership before any cache/budget use.

    This normal (non-yield) dependency closes the short authorization transaction
    before the handler runs; no DB connection is held across external I/O.
    """
    try:
        with engine.begin() as connection:
            identity = _required_session(
                AuthSessionRepository(connection), cookie, csrf=csrf, write=True,
            )
            catalog = TenantCatalogRepository(
                connection, identity, organization_id, session_token=cookie, csrf_token=csrf,
            )
            catalog._authorize("write")
    except TenantAccessDenied as error:
        raise ApiError(403, "organization_forbidden", "Недостаточно прав в организации.") from error
    except SQLAlchemyError as error:
        raise ApiError(503, "company_lookup_authorization_unavailable",
                       "Не удалось проверить права доступа. Повторите позже.") from error
    return organization_id


@lru_cache(maxsize=1)
def get_company_lookup_service() -> CompanyLookupService:
    return CompanyLookupService(LookupSettings.from_environment())


@router.post("/company-lookup", response_model=CompanyLookupResult)
def company_lookup(
    body: CompanyLookupBody,
    response: Response,
    authorized_organization: Annotated[uuid.UUID, Depends(authorize_company_lookup)],
    service: Annotated[CompanyLookupService, Depends(get_company_lookup_service)],
) -> CompanyLookupResult:
    try:
        result = service.lookup(authorized_organization, body.inn)
    except CompanyLookupError as error:
        raise ApiError(error.status_code, error.code, error.message) from error
    response.headers["Cache-Control"] = "no-store"
    return result
