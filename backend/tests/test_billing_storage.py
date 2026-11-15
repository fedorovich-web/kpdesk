"""Storage adapters recheck session authority before every tenant gateway call."""
from unittest.mock import Mock, patch
from uuid import uuid4

from kpdesk.billing.storage import BillingStore, VerifiedBillingStore


def test_prepare_binds_owner_and_passes_no_browser_identity_or_price():
    connection = Mock()
    connection.scalar.return_value = {'order_id': str(uuid4()), 'created_new': True}
    org, key = uuid4(), uuid4()
    with patch('kpdesk.billing.storage.bind_authenticated_tenant') as bind:
        result = BillingStore(connection, org, 'session', 'csrf').prepare_order('start', key)
    bind.assert_called_once_with(connection, session_token='session', organization_id=org, operation='manage', csrf='csrf')
    _sql, params = connection.scalar.call_args.args
    assert set(params) == {'org', 'plan', 'key'}
    assert result['created_new'] is True


def test_summary_uses_read_session_and_unavailable_summary(monkeypatch):
    connection = Mock()
    with patch('kpdesk.billing.storage.bind_authenticated_tenant') as bind, patch('kpdesk.billing.storage.billing_available', return_value=False):
        result = BillingStore(connection, uuid4(), 'session').summary()
    assert bind.call_args.kwargs['operation'] == 'read'
    assert result['available'] is False
    connection.scalar.assert_not_called()


def test_verified_gateway_uses_bound_parameters_and_no_tenant_guc():
    connection = Mock()
    connection.scalar.return_value = {'status': 'paid'}
    result = VerifiedBillingStore(connection).settle_verified(uuid4(), event_id='a'*64,
        event_type='payment.success', invoice_id=uuid4(), status='paid', amount_minor=29900,
        currency='RUB', product_id=None, offer_id=None, buyer_email='owner@example.test', correlation=uuid4())
    assert result == {'status': 'paid'}
    _sql, params = connection.scalar.call_args.args
    assert 'set_config' not in str(_sql)
    assert params['product'] is None and params['offer'] is None



def test_invalid_plan_and_key_cannot_reach_database():
    import pytest

    from kpdesk.billing.storage import BillingConflict
    connection = Mock()
    with patch('kpdesk.billing.storage.bind_authenticated_tenant') as bind:
        with pytest.raises(BillingConflict):
            BillingStore(connection, uuid4(), 'session', 'csrf').prepare_order('browser-price', uuid4())
        with pytest.raises(BillingConflict):
            BillingStore(connection, uuid4(), 'session', 'csrf').prepare_order('start', 'not-a-uuid')
    bind.assert_not_called()
    connection.scalar.assert_not_called()


def test_recheck_session_denial_prevents_invoice_attachment():
    import pytest

    from kpdesk.db.tenant_access import TenantAccessDenied
    connection = Mock()
    with patch('kpdesk.billing.storage.bind_authenticated_tenant', side_effect=TenantAccessDenied('revoked')), pytest.raises(TenantAccessDenied):
        BillingStore(connection, uuid4(), 'session', 'csrf').attach_invoice(uuid4(), uuid4(), 'https://pay.lava.top/test')
    connection.scalar.assert_not_called()


def test_sql_gateway_conflict_cannot_be_misreported_as_success():
    import pytest

    from kpdesk.billing.storage import BillingConflict
    connection = Mock()
    connection.scalar.return_value = {'error': 'mismatch'}
    with pytest.raises(BillingConflict):
        VerifiedBillingStore(connection).settle_verified(uuid4(), event_id='a'*64,
            event_type='payment.success', invoice_id=uuid4(), status='paid', amount_minor=1,
            currency='RUB', product_id=None, offer_id=None, buyer_email='owner@example.test', correlation=uuid4())



def test_reversal_inbox_mismatch_never_appears_successful():
    import pytest

    from kpdesk.billing.storage import BillingConflict
    connection = Mock()
    connection.scalar.return_value = {'error': 'mismatch'}
    with pytest.raises(BillingConflict):
        VerifiedBillingStore(connection).record_reversal(event_id=uuid4(), event_type='refund.success',
            product_id=uuid4(), offer_id=uuid4(), buyer_email='owner@example.test',
            amount_minor=29900, currency='RUB')


def test_reversal_resolution_passes_original_receipt_and_explicit_order():
    connection = Mock()
    connection.scalar.return_value = {'status': 'refunded'}
    event, order, invoice = uuid4(), uuid4(), uuid4()
    result = VerifiedBillingStore(connection).resolve_reversal(event, order, invoice_id=invoice,
        amount_minor=29900, currency='RUB', product_id=None, offer_id=None,
        buyer_email='owner@example.test', correlation=order)
    assert result['status'] == 'refunded'
    _, parameters = connection.scalar.call_args.args
    assert parameters['event'] == event and parameters['order'] == order
    assert parameters['amount'] == 29900 and parameters['correlation'] == order
