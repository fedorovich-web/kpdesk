"""HTTP security contracts with injected synthetic billing service."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from kpdesk.api import billing_routes as routes
from kpdesk.billing.settings import BillingSettings, Offer
from kpdesk.config import AppSettings
from kpdesk.main import create_app


def setup(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr('kpdesk.db.live_service_engines.live_service_engines',lambda:(Mock(),Mock()))
    app=create_app(testing=True,tenant_sessions=True,settings=AppSettings(
        allowed_tenant_origins=('https://kpdesk.ru',)))
    org,order=uuid4(),uuid4(); secret='h'*64
    settings=BillingSettings(enabled=True,api_key='synthetic',webhook_secret=secret,
        offers={key:Offer(uuid4(),uuid4()) for key in ('start','team','business')})
    raw={'order_id': str(order),'plan_id': 'start','status': 'pending','amount_minor': 29900,'currency': 'RUB',
             'payment_url': 'https://pay.lava.top/synthetic','created_at': '2026-11-14T12:00:00Z',
             'buyer_email': 'private@example.test','created_by': str(uuid4()),'product_id': str(uuid4())}
    service=SimpleNamespace(settings=settings,webhook=AsyncMock(),create_order=AsyncMock(return_value=raw),
                            reconcile=AsyncMock(return_value=raw))
    app.dependency_overrides[routes.get_billing_service]=lambda:service
    return TestClient(app,base_url='https://testserver'),service,org,order,secret


def test_only_authenticated_webhook_reaches_processor(monkeypatch: pytest.MonkeyPatch) -> None:
    http,service,_,_,secret=setup(monkeypatch)
    path='/api/billing/lava/webhook'
    with http:
        for headers in ({},{'X-Api-Key':'wrong'}):
            assert http.post(path,json={},headers=headers).status_code==401
        service.webhook.assert_not_awaited()
        response=http.post(path,json={},headers={'X-Api-Key':secret})
        assert response.status_code==204 and response.headers['cache-control']=='no-store'
        assert service.webhook.await_count==1


def test_checkout_rejects_client_price_and_never_exposes_private_order_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    http,service,org,_,_=setup(monkeypatch)
    path=f'/api/session/organizations/{org}/billing/orders'
    headers={'Origin':'https://kpdesk.ru','X-CSRF-Token':'s'*43,'Idempotency-Key':str(uuid4())}
    with http:
        http.cookies.set('__Host-kpdesk_session','x'*43)
        assert http.post(path,json={'plan_id':'start','amount_minor':1},headers=headers).status_code==422
        service.create_order.assert_not_awaited()
        response=http.post(path,json={'plan_id':'start'},headers=headers)
        assert response.status_code==201
        assert response.json()['status']=='pending'
        assert 'buyer_email' not in response.json() and 'product_id' not in response.json()
        assert response.headers['cache-control']=='no-store'


def test_checkout_requires_cookie_csrf_and_idempotency_key(monkeypatch: pytest.MonkeyPatch) -> None:
    http,service,org,_,_=setup(monkeypatch)
    path=f'/api/session/organizations/{org}/billing/orders'
    with http:
        assert http.post(path,json={'plan_id':'start'},headers={'Origin':'https://kpdesk.ru'}).status_code in (401,422)
        service.create_order.assert_not_awaited()


def test_actual_webhook_rejects_chunked_oversize_body(monkeypatch: pytest.MonkeyPatch) -> None:
    http,service,_,_,secret=setup(monkeypatch)
    with http:
        response=http.post('/api/billing/lava/webhook',
            content=iter((b' ' * 32_768,b' ' * 32_769)),
            headers={'X-Api-Key':secret,'Content-Type':'application/json'})
        assert response.status_code==413
        service.webhook.assert_not_awaited()
