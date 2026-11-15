"""Session-bound billing adapters. Caller owns each database transaction."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from kpdesk.billing.plans import PLANS
from kpdesk.billing.schema import billing_available
from kpdesk.db.authenticated_tenant import bind_authenticated_tenant
from kpdesk.db.tenant_access import TenantAccessDenied


class BillingUnavailable(RuntimeError):
    """The reviewed billing schema is absent or operator activation is disabled."""


class BillingConflict(ValueError):
    """Order creation or transition conflicts with persisted server state."""


class BillingStore:
    def __init__(self, connection: Connection, organization_id: UUID,
                 session_token: str, csrf_token: str | None = None) -> None:
        self.connection = connection
        self.organization_id = organization_id
        self.session_token = session_token
        self.csrf_token = csrf_token

    def _bind(self, *, manage: bool = True) -> None:
        bind_authenticated_tenant(self.connection, session_token=self.session_token,
                                  organization_id=self.organization_id,
                                  operation='manage' if manage else 'read',
                                  **({'csrf': self.csrf_token} if manage else {}))

    def _call(self, sql: str, parameters: dict[str, Any]) -> dict[str, Any]:
        result = self.connection.scalar(text(sql), parameters)
        if not isinstance(result, dict):
            raise BillingConflict('Billing request was denied or conflicts with current state.')
        if result.get('error') == 'unavailable':
            raise BillingUnavailable('Billing is not activated.')
        if result.get('error') == 'forbidden':
            raise TenantAccessDenied('Organization membership denied.')
        if result.get('error'):
            raise BillingConflict('Billing request conflicts with current state.')
        return result

    def summary(self) -> dict[str, Any]:
        self._bind(manage=False)
        if not billing_available(self.connection):
            return {'available': False, 'enabled': False, 'plans': [], 'subscription': None,
                    'usage': {'new_quotes': 0, 'catalog_items': 0, 'members': 0}, 'orders': []}
        return self._call('SELECT public.kpdesk_billing_summary(:org)', {'org': self.organization_id})

    def prepare_order(self, plan_id: str, key: UUID) -> dict[str, Any]:
        if plan_id not in PLANS or not isinstance(key, UUID):
            raise BillingConflict('Invalid plan or idempotency key.')
        self._bind()
        return self._call('SELECT public.kpdesk_billing_prepare(:org,:plan,:key)',
                          {'org': self.organization_id, 'plan': plan_id, 'key': key})

    def get_order(self, order_id: UUID) -> dict[str, Any]:
        self._bind()
        return self._call('SELECT public.kpdesk_billing_order(:org,:id)',
                          {'org': self.organization_id, 'id': order_id})

    def attach_invoice(self, order_id: UUID, invoice_id: UUID, payment_url: str) -> dict[str, Any]:
        self._bind()
        return self._call('SELECT public.kpdesk_billing_attach(:org,:id,:invoice,:url)',
                          {'org': self.organization_id, 'id': order_id,
                           'invoice': invoice_id, 'url': payment_url})

    def _mark(self, order_id: UUID, status: str) -> dict[str, Any]:
        self._bind()
        return self._call('SELECT public.kpdesk_billing_mark(:org,:id,:status)',
                          {'org': self.organization_id, 'id': order_id, 'status': status})

    def mark_ambiguous(self, order_id: UUID) -> dict[str, Any]:
        return self._mark(order_id, 'ambiguous')

    def mark_failed(self, order_id: UUID) -> dict[str, Any]:
        return self._mark(order_id, 'failed')


class VerifiedBillingStore:
    """Only kpdesk_auth_runtime can invoke the provider-verified SQL gateways.

    Provider authentication/readback is the service's responsibility. SQL repeats
    immutable order comparisons and enforces idempotent entitlement transitions.
    """
    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def pending_order(self, invoice_id: UUID | None = None,
                      correlation: UUID | None = None) -> dict[str, Any] | None:
        result = self.connection.scalar(text(
            'SELECT public.kpdesk_billing_verified_lookup(:invoice,:correlation)'),
            {'invoice': invoice_id, 'correlation': correlation})
        return result if isinstance(result, dict) else None

    def settle_verified(self, order_id: UUID, *, event_id: str, event_type: str,
                        invoice_id: UUID, status: str, amount_minor: int, currency: str,
                        product_id: UUID | None, offer_id: UUID | None, buyer_email: str,
                        correlation: UUID, payment_url: str | None = None) -> dict[str, Any]:
        result = self.connection.scalar(text('''SELECT public.kpdesk_billing_settle(
            :id,:event,:type,:invoice,:status,:amount,:currency,:product,:offer,:email,:correlation,:url)'''),
            {'id': order_id, 'event': event_id, 'type': event_type, 'invoice': invoice_id,
             'status': status, 'amount': amount_minor, 'currency': currency,
             'product': product_id, 'offer': offer_id, 'email': buyer_email,
             'correlation': correlation, 'url': payment_url})
        if not isinstance(result, dict) or result.get('error'):
            raise BillingConflict('Verified invoice does not match the stored order.')
        return result

    def record_reversal(self, *, event_id: UUID, event_type: str, product_id: UUID,
                        offer_id: UUID, buyer_email: str, amount_minor: int,
                        currency: str) -> dict[str, Any]:
        result = self.connection.scalar(text('''SELECT public.kpdesk_billing_record_reversal(
            :event,:type,:product,:offer,:email,:amount,:currency)'''),
            {'event': event_id, 'type': event_type, 'product': product_id, 'offer': offer_id,
             'email': buyer_email, 'amount': amount_minor, 'currency': currency})
        if not isinstance(result, dict) or result.get('error'):
            raise BillingConflict('Reversal event conflicts with its stored identity.')
        return result

    def get_reversal(self, event_id: UUID) -> dict[str, Any] | None:
        result = self.connection.scalar(text('SELECT public.kpdesk_billing_get_reversal(:event)'),
                                        {'event': event_id})
        return result if isinstance(result, dict) else None

    def resolve_reversal(self, event_id: UUID, order_id: UUID, *, invoice_id: UUID,
                         amount_minor: int, currency: str, product_id: UUID | None,
                         offer_id: UUID | None, buyer_email: str, correlation: UUID,
                         payment_url: str | None = None) -> dict[str, Any]:
        result = self.connection.scalar(text('''SELECT public.kpdesk_billing_resolve_reversal(
            :event,:order,:invoice,:amount,:currency,:product,:offer,:email,:correlation,:url)'''),
            {'event': event_id, 'order': order_id, 'invoice': invoice_id,
             'amount': amount_minor, 'currency': currency, 'product': product_id,
             'offer': offer_id, 'email': buyer_email, 'correlation': correlation, 'url': payment_url})
        if not isinstance(result, dict) or result.get('error'):
            raise BillingConflict('Reversal cannot be linked to the verified order.')
        return result
