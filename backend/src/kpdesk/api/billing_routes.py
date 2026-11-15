"""Owner checkout and authenticated PSP notifications, never browser-paid flags."""
import hmac
import re
from collections.abc import Awaitable
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from starlette.formparsers import MultiPartException

from kpdesk.api.errors import ApiError
from kpdesk.api.tenant_session_routes import (
    SESSION_COOKIE,
    get_unreleased_catalog_engine,
    get_unreleased_login_engine,
)
from kpdesk.billing.lava import LavaClient, LavaError, _payment_url
from kpdesk.billing.service import BillingService, DatabaseBillingRepository
from kpdesk.billing.settings import BillingSettings
from kpdesk.billing.storage import BillingConflict, BillingUnavailable
from kpdesk.db.auth_sessions import SessionDenied
from kpdesk.db.tenant_access import TenantAccessDenied

router = APIRouter()
BASE = '/api/session/organizations/{organization_id}/billing'
COOKIE = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]
CSRF = Annotated[str, Header(alias='X-CSRF-Token', min_length=43, max_length=43)]
PlanId = Literal['start', 'team', 'business']


def get_billing_settings() -> BillingSettings:
    try:
        return BillingSettings.from_env()
    except (TypeError, ValueError):
        raise ApiError(503, 'billing_configuration_invalid', 'Оплата временно недоступна.') from None


def get_billing_service(
    settings: Annotated[BillingSettings, Depends(get_billing_settings)],
    tenant: Annotated[Engine, Depends(get_unreleased_catalog_engine)],
    auth: Annotated[Engine, Depends(get_unreleased_login_engine)],
) -> BillingService:
    return BillingService(settings, DatabaseBillingRepository(tenant, auth), LavaClient(settings))


SERVICE = Annotated[BillingService, Depends(get_billing_service)]


class OrderBody(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    plan_id: PlanId


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class BillingOrder(BaseModel):
    order_id: UUID
    plan_id: PlanId
    status: Literal['prepared', 'creating', 'pending', 'paid', 'failed', 'ambiguous', 'revoked']
    amount_minor: int = Field(gt=0)
    currency: Literal['RUB']
    payment_url: str | None
    created_at: datetime


class BillingPlan(BaseModel):
    id: PlanId
    name: str
    price_minor: int = Field(gt=0)
    people_limit: int = Field(gt=0)
    new_quotes: int = Field(gt=0)
    catalog_items: int = Field(gt=0)
    days: Literal[30]


class Subscription(BaseModel):
    plan_id: PlanId
    starts_at: datetime
    ends_at: datetime


class Usage(BaseModel):
    new_quotes: int = Field(ge=0)
    catalog_items: int = Field(ge=0)
    members: int = Field(ge=0)


class BillingSummary(BaseModel):
    available: bool
    enabled: bool
    plans: list[BillingPlan]
    subscription: Subscription | None
    usage: Usage
    orders: list[BillingOrder]


def _cookie(value: str | None) -> str:
    if value is None or re.fullmatch(r'[A-Za-z0-9_-]{43}', value) is None:
        raise ApiError(401, 'invalid_session', 'Для продолжения войдите в KPDesk.')
    return value


def _order(value: dict[str, Any]) -> BillingOrder:
    try:
        fields = {name: value.get(name) for name in BillingOrder.model_fields}
        if fields['status'] in ('refunded', 'chargeback'):
            fields['status'] = 'revoked'
        if fields['payment_url'] is not None:
            fields['payment_url'] = _payment_url(fields['payment_url'])
        return BillingOrder.model_validate(fields)
    except (ValueError, TypeError):
        raise ApiError(503, 'billing_data_invalid', 'Не удалось проверить данные оплаты.') from None


async def _invoke[T](operation: Awaitable[T]) -> T:
    try:
        return await operation
    except TenantAccessDenied as error:
        if isinstance(error.__cause__, SessionDenied):
            raise ApiError(401, 'invalid_session', 'Для продолжения войдите в KPDesk.') from error
        raise ApiError(403, 'billing_forbidden', 'У вас нет прав на это действие с оплатой.') from error
    except BillingUnavailable as error:
        raise ApiError(503, 'billing_unavailable', 'Оплата пока недоступна.') from error
    except BillingConflict as error:
        raise ApiError(409, 'billing_order_conflict', 'Проверьте текущий тариф и ранее созданные счета.') from error
    except (SQLAlchemyError, LavaError) as error:
        raise ApiError(503, 'billing_unavailable', 'Не удалось проверить оплату. Повторите проверку позже.') from error


@router.get(BASE, response_model=BillingSummary)
async def summary(organization_id: UUID, service: SERVICE, cookie: COOKIE = None) -> BillingSummary:
    value = await _invoke(service.summary(organization_id, _cookie(cookie)))
    try:
        return BillingSummary.model_validate({**value, 'orders': [_order(v) for v in value['orders']]})
    except (ValidationError, KeyError, TypeError):
        raise ApiError(503, 'billing_data_invalid', 'Не удалось проверить данные оплаты.') from None


@router.post(BASE+'/orders', response_model=BillingOrder, status_code=201)
async def create_order(organization_id: UUID, body: OrderBody, service: SERVICE, csrf: CSRF,
    key: Annotated[UUID, Header(alias='Idempotency-Key')], cookie: COOKIE = None) -> BillingOrder:
    return _order(await _invoke(service.create_order(organization_id, _cookie(cookie), csrf, body.plan_id, key)))


@router.post(BASE+'/orders/{order_id}/reconcile', response_model=BillingOrder)
async def reconcile(organization_id: UUID, order_id: UUID, body: EmptyBody, service: SERVICE,
                    csrf: CSRF, cookie: COOKIE = None) -> BillingOrder:
    return _order(await _invoke(service.reconcile(organization_id, _cookie(cookie), csrf, order_id)))


@router.post('/api/billing/lava/webhook', status_code=204)
async def webhook(request: Request, service: SERVICE,
                  api_key: Annotated[str | None, Header(alias='X-Api-Key')] = None) -> Response:
    settings = service.settings
    if not settings.enabled:
        raise ApiError(503, 'billing_disabled', 'Оплата пока не подключена.')
    if (api_key is None or len(api_key) > 80
            or not hmac.compare_digest(api_key.encode('utf-8'), settings.webhook_secret.encode('ascii'))):
        raise ApiError(401, 'billing_webhook_unauthorized', 'Не удалось подтвердить источник уведомления.')
    if request.headers.get('content-type', '').split(';', 1)[0].lower() != 'application/json':
        raise ApiError(415, 'billing_webhook_content_type', 'Ожидается JSON.')
    content = bytearray()
    try:
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 65_536:
                raise ApiError(413, 'billing_body_too_large', 'Уведомление превышает лимит.')
    except MultiPartException:
        if not getattr(request.state, 'body_limit_exceeded', False):
            raise
        raise ApiError(413, 'billing_body_too_large', 'Уведомление превышает лимит.') from None
    await _invoke(service.webhook(bytes(content)))
    return Response(status_code=204)
