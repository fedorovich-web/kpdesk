"""Committed order claims precede remote calls; only verified receipts grant access."""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from decimal import Decimal
from functools import partial
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import Engine

from kpdesk.api.errors import ApiError
from kpdesk.async_work import run_thread, settle_thread
from kpdesk.billing.lava import LavaClient, LavaError, VerifiedInvoice, _money, _uuid
from kpdesk.billing.plans import PLANS
from kpdesk.billing.settings import BillingSettings
from kpdesk.billing.storage import BillingStore, VerifiedBillingStore

_EVENTS = frozenset(('payment.success', 'payment.failed', 'refund.success',
    'chargeback.initiated', 'subscription.recurring.payment.success',
    'subscription.recurring.payment.failed', 'subscription.cancelled'))


def _refuse(status: int = 409, code: str = 'billing_invoice_mismatch') -> ApiError:
    return ApiError(status, code, 'Не удалось подтвердить оплату. Обновите статус или обратитесь в поддержку.')


def _unique_json(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_key')
        result[key] = value
    return result


@dataclass(frozen=True)
class Webhook:
    event: str
    invoice_id: UUID
    product_id: UUID
    email: str
    amount_minor: int
    currency: str


@dataclass(frozen=True)
class Reversal:
    event_id: UUID
    event_type: str
    product_id: UUID
    offer_id: UUID
    buyer_email: str
    amount_minor: int
    currency: str


def parse_webhook(raw: bytes) -> Webhook | Reversal | None:
    if len(raw) > 65_536:
        raise _refuse(413, 'billing_body_too_large')
    try:
        value = json.loads(raw, parse_float=Decimal, object_pairs_hook=_unique_json)
        if not isinstance(value, dict) or ('eventType' in value and 'event_type' in value):
            raise TypeError
        if 'event_type' in value:
            kind=value['event_type']
            if not isinstance(kind,str):
                raise TypeError
            if kind not in ('refund.success','chargeback.initiated'):
                return None
            data=value['data']; product=data['product']; email=data['customer_email']
            amount=_money(data['amount'])
            if (not isinstance(email,str) or not 3 <= len(email) <= 254
                    or '\x00' in email or amount <= 0 or data['currency'] != 'RUB'):
                raise ValueError
            # These documented events have no invoice/correlation UUID. Retain
            # them durably for explicit operator correlation, never guess a sale.
            return Reversal(_uuid(value['event_id']),kind,_uuid(product['product_id']),
                _uuid(product['tier_id']),email.strip().lower(),amount,'RUB')
        if not isinstance(value.get('eventType'),str):
            raise TypeError
        if value['eventType'] not in _EVENTS:
            return None
        # The configured product is ONE_TIME. Recurring events cannot add grants.
        if value['eventType'].startswith('subscription.'):
            return None
        product, buyer = value['product'], value['buyer']
        if not isinstance(product, dict) or not isinstance(buyer, dict):
            raise TypeError
        email = buyer['email']
        if not isinstance(email, str) or not 3 <= len(email) <= 254 or '\x00' in email:
            raise ValueError
        currency = value['currency']
        if currency != 'RUB':
            raise ValueError
        amount = _money(value['amount'])
        if amount <= 0:
            raise ValueError
        return Webhook(value['eventType'], _uuid(value['contractId']), _uuid(product['id']),
                       email.strip().lower(), amount, currency)
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError, ArithmeticError):
        raise _refuse(422, 'billing_webhook_invalid') from None


class Repository(Protocol):
    def tenant(self, org: UUID, session: str, csrf: str | None, action: str,
               *args: Any) -> dict[str, Any]: ...
    def verified(self, action: str, *args: Any, **kwargs: Any) -> dict[str, Any] | None: ...


class DatabaseBillingRepository:
    def __init__(self, tenant_engine: Engine, auth_engine: Engine) -> None:
        self.tenant_engine, self.auth_engine = tenant_engine, auth_engine

    def tenant(self, org: UUID, session: str, csrf: str | None, action: str,
               *args: Any) -> dict[str, Any]:
        if action not in {'summary', 'prepare_order', 'get_order', 'attach_invoice',
                          'mark_ambiguous', 'mark_failed'}:
            raise ValueError('billing_operation_invalid')
        with self.tenant_engine.begin() as connection:
            store = BillingStore(connection, org, session, csrf)
            operation: Callable[..., dict[str, Any]] = getattr(store, action)
            return operation(*args)

    def verified(self, action: str, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        if action not in {'pending_order', 'settle_verified', 'record_reversal',
                          'get_reversal', 'resolve_reversal'}:
            raise ValueError('billing_operation_invalid')
        with self.auth_engine.begin() as connection:
            operation: Callable[..., dict[str, Any] | None] = getattr(VerifiedBillingStore(connection), action)
            return operation(*args, **kwargs)


class BillingService:
    def __init__(self, settings: BillingSettings, repository: Repository, client: LavaClient) -> None:
        self.settings, self.repository, self.client = settings, repository, client

    def enabled(self) -> None:
        if not self.settings.enabled:
            raise _refuse(503, 'billing_disabled')

    async def summary(self, org: UUID, session: str) -> dict[str, Any]:
        value = await run_thread(partial(self.repository.tenant, org, session, None, 'summary'))
        if bool(value['enabled']) != self.settings.enabled:
            raise _refuse(503, 'billing_configuration_mismatch')
        return {**value, 'plans': [asdict(plan) for plan in PLANS.values()]}

    async def create_order(self, org: UUID, session: str, csrf: str, plan: str,
                           key: UUID) -> dict[str, Any]:
        self.enabled()
        if plan not in PLANS:
            raise _refuse(422, 'billing_plan_invalid')
        order, cancelled = await settle_thread(partial(
            self.repository.tenant, org, session, csrf, 'prepare_order', plan, key))
        order_id = UUID(str(order['order_id']))
        if cancelled is not None:
            if order['created_new']:
                await settle_thread(partial(self.repository.tenant, org, session, csrf,
                                            'mark_failed', order_id))
            raise cancelled
        if not order['created_new']:
            return order
        offer = self.settings.offers[plan]
        if (UUID(str(order['product_id'])) != offer.product_id
                or UUID(str(order['offer_id'])) != offer.offer_id
                or order['amount_minor'] != PLANS[plan].price_minor or order['currency'] != 'RUB'):
            await run_thread(partial(self.repository.tenant, org, session, csrf, 'mark_failed', order_id))
            raise _refuse(503, 'billing_configuration_mismatch')
        try:
            invoice = await self.client.create_invoice(order['buyer_email'], offer.offer_id,
                                                       order['amount_minor'], order_id)
        except asyncio.CancelledError:
            await settle_thread(partial(self.repository.tenant, org, session, csrf,
                                        'mark_ambiguous', order_id))
            raise
        except LavaError as error:
            mark = 'mark_ambiguous' if error.ambiguous else 'mark_failed'
            return await run_thread(partial(self.repository.tenant, org, session, csrf, mark, order_id))
        # A second transaction rechecks the owner's current session/CSRF/membership.
        return await run_thread(partial(self.repository.tenant, org, session, csrf,
            'attach_invoice', order_id, invoice.invoice_id, invoice.payment_url))

    @staticmethod
    def _match(order: dict[str, Any], verified: VerifiedInvoice) -> None:
        if (UUID(str(order['order_id'])) != verified.correlation
                or (order.get('invoice_id') is not None
                    and UUID(str(order['invoice_id'])) != verified.invoice_id)
                or order['amount_minor'] != verified.amount_minor
                or order['currency'] != verified.currency
                or order['buyer_email'].lower() != verified.email.strip().lower()
                or (verified.product_id is not None
                    and UUID(str(order['product_id'])) != verified.product_id)
                or (verified.offer_id is not None
                    and UUID(str(order['offer_id'])) != verified.offer_id)):
            raise _refuse()

    async def _settle(self, order: dict[str, Any], verified: VerifiedInvoice, *,
                      event_id: str, event_type: str, status: str,
                      product_id: UUID | None = None) -> dict[str, Any]:
        result = await run_thread(partial(self.repository.verified, 'settle_verified',
            UUID(str(order['order_id'])), event_id=event_id, event_type=event_type,
            invoice_id=verified.invoice_id, status=status, amount_minor=verified.amount_minor,
            currency=verified.currency, product_id=product_id, offer_id=verified.offer_id,
            buyer_email=verified.email.strip().lower(), correlation=verified.correlation))
        if result is None:
            raise _refuse()
        return result

    async def reconcile(self, org: UUID, session: str, csrf: str, order_id: UUID) -> dict[str, Any]:
        self.enabled()
        order = await run_thread(partial(self.repository.tenant, org, session, csrf, 'get_order', order_id))
        if not order.get('invoice_id'):
            raise _refuse(409, 'billing_invoice_unknown')
        verified = await self.client.verify_invoice(UUID(str(order['invoice_id'])))
        if UUID(str(order['invoice_id'])) != verified.invoice_id:
            raise _refuse()
        # Revalidate after waiting on an external service, before settlement.
        order = await run_thread(partial(self.repository.tenant, org, session, csrf, 'get_order', order_id))
        self._match(order, verified)
        if verified.status in ('new', 'in_progress'):
            return order
        status = 'paid' if verified.status == 'completed' else 'failed'
        fingerprint = hashlib.sha256(f'reconcile:{verified.invoice_id}:{status}'.encode()).hexdigest()
        return await self._settle(order, verified, event_id=fingerprint,
                                  event_type='payment.success' if status == 'paid' else 'payment.failed', status=status)

    async def webhook(self, raw: bytes) -> None:
        self.enabled()
        event = parse_webhook(raw)
        if event is None:
            return
        if isinstance(event, Reversal):
            await run_thread(partial(self.repository.verified,'record_reversal',**asdict(event)))
            return
        verified = await self.client.verify_invoice(event.invoice_id)
        if verified.invoice_id != event.invoice_id:
            raise _refuse()
        order = await run_thread(partial(self.repository.verified, 'pending_order',
            correlation=verified.correlation))
        if order is None:
            return  # Authenticated provider event belongs to no KPDesk order.
        self._match(order, verified)
        reverse = event.event in ('refund.success', 'chargeback.initiated')
        if (UUID(str(order['product_id'])) != event.product_id
                or event.email != verified.email.strip().lower()
                or event.currency != verified.currency
                or (event.amount_minor > verified.amount_minor if reverse
                    else event.amount_minor != verified.amount_minor)):
            raise _refuse()
        if reverse:
            status = 'refunded' if event.event == 'refund.success' else 'chargeback'
        elif verified.status in ('new', 'in_progress'):
            raise _refuse(503, 'billing_invoice_pending')
        else:
            status = 'paid' if verified.status == 'completed' else 'failed'
        fingerprint = hashlib.sha256(raw+b'|'+status.encode()).hexdigest()
        await self._settle(order, verified, event_id=fingerprint, event_type=event.event,
                           status=status, product_id=event.product_id)

    async def resolve_reversal(self, event_id: UUID, invoice_id: UUID) -> dict[str, Any]:
        """Operator-only: bind a retained reversal to its documented invoice.

        No public route exposes this method. Receipt readback and the database
        repeat immutable comparisons; email/product never select an order.
        """
        self.enabled()
        event=await run_thread(partial(self.repository.verified,'get_reversal',event_id))
        if event is None:
            raise _refuse(404,'billing_reversal_unknown')
        verified=await self.client.verify_invoice(invoice_id)
        if verified.invoice_id != invoice_id:
            raise _refuse()
        order=await run_thread(partial(self.repository.verified,'pending_order',correlation=verified.correlation))
        if order is None:
            raise _refuse()
        self._match(order,verified)
        result=await run_thread(partial(self.repository.verified,'resolve_reversal',event_id,
            UUID(str(order['order_id'])),invoice_id=invoice_id,amount_minor=verified.amount_minor,
            currency=verified.currency,product_id=verified.product_id,offer_id=verified.offer_id,
            buyer_email=verified.email.strip().lower(),correlation=verified.correlation))
        if result is None:
            raise _refuse()
        return result
