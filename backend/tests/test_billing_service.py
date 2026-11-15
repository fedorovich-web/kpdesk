"""Payment orchestration uses synthetic gateways/transports, never live I/O."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from kpdesk.api.errors import ApiError
from kpdesk.billing.lava import InvoiceResult, LavaError, VerifiedInvoice
from kpdesk.billing.service import BillingService, parse_webhook
from kpdesk.billing.settings import BillingSettings, Offer
from kpdesk.db.tenant_access import TenantAccessDenied


class Repository:
    def __init__(self) -> None:
        self.org, self.id, self.invoice, self.product, self.offer = (uuid4() for _ in range(5))
        self.order = {'order_id': str(self.id), 'organization_id': str(self.org), 'plan_id': 'start',
            'amount_minor': 29900, 'currency': 'RUB', 'buyer_email': 'owner@example.test',
            'product_id': str(self.product), 'offer_id': str(self.offer), 'status': 'creating',
            'invoice_id': None, 'payment_url': None, 'created_at': '2026-11-14T12:00:00+00:00'}
        self.calls: list[str] = []
        self.new, self.denied, self.settled = True, False, []
        self.reversals: dict[str, dict[str, object]] = {}

    def tenant(self, org, session, csrf, action, *args):  # type: ignore[no-untyped-def]
        self.calls.append(action)
        if self.denied:
            raise TenantAccessDenied('synthetic denied')
        if action == 'summary':
            return {'available': True, 'enabled': True, 'subscription': None,
                        'usage': {'new_quotes': 0,'catalog_items': 0,'members': 1},'orders': []}
        if action == 'prepare_order':
            result={**self.order,'created_new':self.new};self.new=False;return result
        if action == 'attach_invoice':
            self.order.update(invoice_id=str(args[1]),payment_url=args[2],status='pending')
        elif action in ('mark_ambiguous','mark_failed'):
            self.order['status']='ambiguous' if action=='mark_ambiguous' else 'failed'
        return dict(self.order)

    def verified(self, action, *args, **kwargs):  # type: ignore[no-untyped-def]
        if action=='pending_order':return dict(self.order)
        if action=='record_reversal':
            self.reversals[str(kwargs['event_id'])]=kwargs
            return kwargs
        if action=='get_reversal':return self.reversals.get(str(args[0]))
        if action=='resolve_reversal':
            kwargs['status']='refunded' if self.reversals[str(args[0])]['event_type']=='refund.success' else 'chargeback'
        self.settled.append(kwargs)
        self.order['status']=kwargs['status']
        return dict(self.order)


def setup():  # type: ignore[no-untyped-def]
    repo=Repository()
    settings=BillingSettings(enabled=True,api_key='synthetic-provider',webhook_secret='w'*64,
        offers={plan:Offer(repo.product,repo.offer) for plan in ('start','team','business')})
    client=SimpleNamespace(create_invoice=AsyncMock(return_value=InvoiceResult(repo.invoice,
        'https://pay.lava.top/synthetic')), verify_invoice=AsyncMock(return_value=VerifiedInvoice(
            repo.invoice,'completed',29900,'RUB','owner@example.test',repo.id)))
    return repo,client,BillingService(settings,repo,client)  # type: ignore[arg-type]


def event(repo: Repository, **changes: object) -> bytes:
    value={'eventType': 'payment.success','contractId': str(repo.invoice),'amount': 299,'currency': 'RUB',
               'product': {'id':str(repo.product)},'buyer': {'email':'owner@example.test'}}
    value.update(changes)
    return json.dumps(value).encode()


@pytest.mark.asyncio
async def test_invoice_claim_precedes_provider_and_replays_do_not_post() -> None:
    repo,provider,service=setup()
    result=await service.create_order(repo.org,'cookie','csrf','start',uuid4())
    assert repo.calls==['prepare_order','attach_invoice'] and result['status']=='pending'
    provider.create_invoice.assert_awaited_once_with('owner@example.test',repo.offer,29900,repo.id)
    await service.create_order(repo.org,'cookie','csrf','start',uuid4())
    assert provider.create_invoice.await_count==1


@pytest.mark.asyncio
@pytest.mark.parametrize('ambiguous',[True,False])
async def test_failed_provider_creation_is_persisted_without_automatic_retry(ambiguous: bool) -> None:
    repo,provider,service=setup()
    provider.create_invoice.side_effect=LavaError('provider_unavailable',ambiguous=ambiguous)
    result=await service.create_order(repo.org,'cookie','csrf','start',uuid4())
    assert result['status']==('ambiguous' if ambiguous else 'failed')
    assert provider.create_invoice.await_count==1


@pytest.mark.asyncio
async def test_http_cancellation_preserves_ambiguous_order_before_propagating() -> None:
    repo,provider,service=setup();provider.create_invoice.side_effect=asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await service.create_order(repo.org,'cookie','csrf','start',uuid4())
    assert repo.order['status']=='ambiguous'


@pytest.mark.asyncio
async def test_invoice_attachment_rechecks_authority_after_remote_await() -> None:
    repo,provider,service=setup()
    async def remote(*args):  # type: ignore[no-untyped-def]
        repo.denied=True
        return InvoiceResult(repo.invoice,'https://pay.lava.top/synthetic')
    provider.create_invoice.side_effect=remote
    with pytest.raises(TenantAccessDenied):
        await service.create_order(repo.org,'cookie','csrf','start',uuid4())
    assert repo.order['invoice_id'] is None


@pytest.mark.asyncio
async def test_webhook_matches_internal_identity_and_provider_readback_before_grant() -> None:
    repo,provider,service=setup()
    await service.webhook(event(repo))
    assert len(repo.settled)==1 and repo.settled[0]['status']=='paid'
    assert repo.settled[0]['product_id']==repo.product
    assert repo.settled[0]['correlation']==repo.id
    provider.create_invoice.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('amount_minor',100),('currency','USD'),
    ('email','other@example.test'),('correlation',uuid4()),('invoice_id',uuid4())])
async def test_forged_readback_cannot_settle(field: str,value: object) -> None:
    repo,provider,service=setup()
    provider.verify_invoice.return_value=replace(provider.verify_invoice.return_value,**{field:value})
    with pytest.raises(ApiError):await service.webhook(event(repo))
    assert not repo.settled


@pytest.mark.asyncio
@pytest.mark.parametrize('change',[{'amount':1},{'currency':'USD'},
    {'product':{'id':str(uuid4())}},{'buyer':{'email':'other@example.test'}}])
async def test_signed_but_unmatched_webhook_does_not_settle(change: dict[str,object]) -> None:
    repo,_,service=setup()
    with pytest.raises(ApiError):await service.webhook(event(repo,**change))
    assert not repo.settled


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['refund.success','chargeback.initiated'])
async def test_documented_reversal_is_retained_until_explicit_invoice_correlation(kind: str) -> None:
    repo,provider,service=setup(); event_id=uuid4()
    raw=json.dumps({'event_id':str(event_id),'event_type':kind,'data':{
        'refund_id' if kind=='refund.success' else 'chargeback_id':str(uuid4()),
        'amount':100,'currency':'RUB','customer_email':'owner@example.test',
        'product':{'product_id':str(repo.product),'tier_id':str(repo.offer)}}}).encode()
    await service.webhook(raw)
    assert str(event_id) in repo.reversals and not repo.settled
    provider.verify_invoice.assert_not_awaited()
    await service.resolve_reversal(event_id,repo.invoice)
    assert repo.settled[0]['status']==('refunded' if kind=='refund.success' else 'chargeback')
    assert repo.settled[0]['amount_minor']==29900


@pytest.mark.asyncio
async def test_unrecorded_reversal_cannot_be_resolved() -> None:
    repo,provider,service=setup()
    with pytest.raises(ApiError):await service.resolve_reversal(uuid4(),repo.invoice)
    provider.verify_invoice.assert_not_awaited()
    assert not repo.settled


def test_reversal_rejects_conflicting_event_formats() -> None:
    with pytest.raises(ApiError):
        parse_webhook(b'{"eventType":"payment.success","event_type":"refund.success"}')


@pytest.mark.parametrize('field',['contractId','product'])
def test_payment_uuid_and_object_types_are_rejected_safely(field: str) -> None:
    repo=Repository()
    change={field:42 if field=='contractId' else {'id':42}}
    with pytest.raises(ApiError) as error:parse_webhook(event(repo,**change))
    assert error.value.status_code==422


@pytest.mark.asyncio
async def test_unknown_event_has_no_provider_or_database_side_effect() -> None:
    repo,provider,service=setup()
    await service.webhook(b'{"eventType":"future.unknown"}')
    provider.verify_invoice.assert_not_awaited()
    assert not repo.calls and not repo.settled


@pytest.mark.asyncio
async def test_disabled_service_cannot_create_or_process_webhook() -> None:
    repo,provider,_=setup();service=BillingService(BillingSettings(),repo,provider)  # type: ignore[arg-type]
    for operation in (service.create_order(repo.org,'c','s','start',uuid4()),service.webhook(event(repo))):
        with pytest.raises(ApiError) as error:await operation
        assert error.value.status_code==503
    provider.create_invoice.assert_not_awaited();provider.verify_invoice.assert_not_awaited()


def test_parser_rejects_duplicates_and_recursive_json_without_leaking_body() -> None:
    for raw in [b'{"eventType":"future","eventType":"payment.success"}',b'['*20000+b'0'+b']'*20000]:
        with pytest.raises(ApiError) as error:parse_webhook(raw)
        assert error.value.status_code==422
