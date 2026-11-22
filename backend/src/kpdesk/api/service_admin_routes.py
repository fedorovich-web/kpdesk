"""Read-only, same-session API for the service owner."""
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Query
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from kpdesk.api.errors import ApiError
from kpdesk.api.tenant_session_routes import SESSION_COOKIE, get_unreleased_catalog_engine
from kpdesk.db.auth_sessions import SessionDenied, _token_digest
from kpdesk.db.service_admin_schema import service_admin_available

router = APIRouter(prefix='/api/admin', include_in_schema=False)
TenantEngine = Annotated[Engine, Depends(get_unreleased_catalog_engine)]
SessionCookie = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]
Section = Literal['capability', 'overview', 'users', 'organizations', 'payments', 'reversals']
Days = Literal['7', '30', '90']
Count = Annotated[int, Field(ge=0)]
MinorUnits = Annotated[str, Field(pattern=r'^\d+$')]


class Capability(BaseModel):
    allowed: bool


class Amount(BaseModel):
    currency: str
    count: Count
    amount_minor: MinorUnits


class PlanCount(BaseModel):
    plan_id: str
    count: Count


class Overview(BaseModel):
    as_of: datetime
    period_days: Literal[7, 30, 90]
    users_total: Count
    users_new: Count
    users_verified: Count
    organizations_total: Count
    organizations_active: Count
    paid_organizations: Count
    expiring_7: Count
    plans: list[PlanCount]
    receipts: list[Amount]
    refunds: list[Amount]
    unresolved_reversals: Count
    ambiguous_orders: Count
    quotes_new: Count
    organizations_with_catalog: Count
    ai_attempts: Count
    ai_success: Count


class UserRow(BaseModel):
    id: UUID
    email: str
    status: str
    created_at: datetime
    email_verified_at: datetime | None
    organizations_count: Count


class OrganizationRow(BaseModel):
    id: UUID
    name: str
    status: str
    created_at: datetime
    members: Count
    quotes: Count
    plan_id: str | None
    paid_through: datetime | None


class PaymentRow(BaseModel):
    order_id: UUID
    organization_name: str
    plan_id: str
    amount_minor: MinorUnits
    currency: str
    status: str
    created_at: datetime
    confirmed_at: datetime | None


class ReversalRow(BaseModel):
    event_id: UUID
    event_type: str
    amount_minor: MinorUnits
    currency: str
    received_at: datetime
    resolved: bool
    order_id: UUID | None


class Page[Row](BaseModel):
    total: Count
    items: Annotated[list[Row], Field(max_length=25)]


class PageQuery(BaseModel):
    q: str = Field(default='', max_length=100)
    page: int = Field(default=1, ge=1, le=1000)


def unavailable() -> ApiError:
    return ApiError(503, 'service_admin_unavailable', 'Не удалось загрузить кабинет владельца.')


def read_admin(engine: Engine, digest: str, section: Section, days: int, query: str, page: int) -> object:
    with engine.begin() as connection:
        connection.exec_driver_sql('SET TRANSACTION READ ONLY')
        connection.exec_driver_sql("SET LOCAL statement_timeout='5s'")
        if not service_admin_available(connection):
            if section == 'capability':
                return {'access': 'forbidden'}
            raise unavailable()
        return connection.scalar(text("""
            SELECT public.kpdesk_service_admin_read(:digest,:section,:days,:query,:page)
        """), {'digest': digest, 'section': section, 'days': days, 'query': query, 'page': page})


def _response[Result: BaseModel](
    model: type[Result], engine: Engine, cookie: str | None, section: Section,
    *, days: int = 30, query: str = '', page: int = 1,
) -> Result:
    try:
        digest = _token_digest(cookie or '')
    except SessionDenied:
        raise ApiError(401, 'invalid_session', 'Войдите в KPDesk.') from None
    try:
        result = read_admin(engine, digest, section, days, query, page)
        if not isinstance(result, dict):
            raise unavailable()
        match result.get('access'):
            case 'invalid_session':
                raise ApiError(401, 'invalid_session', 'Войдите в KPDesk.')
            case 'forbidden':
                if section == 'capability':
                    return model.model_validate({'allowed': False})
                raise ApiError(403, 'service_admin_forbidden', 'Этот кабинет доступен владельцу KPDesk.')
            case 'allowed':
                return model.model_validate(result.get('data'))
            case _:
                raise unavailable()
    except (SQLAlchemyError, ValidationError):
        raise unavailable() from None


@router.get('/capability')
def capability(engine: TenantEngine, cookie: SessionCookie = None) -> Capability:
    return _response(Capability, engine, cookie, 'capability')


@router.get('/overview')
def overview(engine: TenantEngine, days: Days = '30', cookie: SessionCookie = None) -> Overview:
    return _response(Overview, engine, cookie, 'overview', days=int(days))


Paging = Annotated[PageQuery, Query()]


@router.get('/users')
def users(engine: TenantEngine, paging: Paging, cookie: SessionCookie = None) -> Page[UserRow]:
    return _response(Page[UserRow], engine, cookie, 'users', query=paging.q.strip().lower(), page=paging.page)


@router.get('/organizations')
def organizations(engine: TenantEngine, paging: Paging, cookie: SessionCookie = None) -> Page[OrganizationRow]:
    return _response(Page[OrganizationRow], engine, cookie, 'organizations', query=paging.q.strip().lower(), page=paging.page)


@router.get('/payments')
def payments(engine: TenantEngine, paging: Paging, cookie: SessionCookie = None) -> Page[PaymentRow]:
    return _response(Page[PaymentRow], engine, cookie, 'payments', query=paging.q.strip().lower(), page=paging.page)


@router.get('/reversals')
def reversals(engine: TenantEngine, paging: Paging, cookie: SessionCookie = None) -> Page[ReversalRow]:
    return _response(Page[ReversalRow], engine, cookie, 'reversals', query=paging.q.strip().lower(), page=paging.page)
