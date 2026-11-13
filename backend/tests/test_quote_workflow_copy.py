"""Offline error transport: preserve refusal codes, hide internal diagnostics."""
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError
from test_quote_pdf import draft

from kpdesk.api import persisted_quote_routes, tenant_quote_routes
from kpdesk.api.errors import ApiError
from kpdesk.db.tenant_access import TenantAccessDenied
from kpdesk.quotes.pdf import QuotePDFError
from kpdesk.quotes.review_audit import QuoteReviewDenied
from kpdesk.quotes.storage import QuoteConflict, QuoteNotFound


@pytest.mark.parametrize(('error', 'status', 'code'), [
    (TenantAccessDenied('private-sentinel'), 403, 'organization_forbidden'),
    (QuoteNotFound('private-sentinel'), 404, 'quote_not_found'),
    (QuoteReviewDenied('private-sentinel'), 409, 'review_attestation_denied'),
    (ValueError('private-sentinel'), 422, 'invalid_quote'),
    (SQLAlchemyError('private-sentinel'), 503, 'quote_store_unavailable'),
    (RuntimeError('private-sentinel'), 503, 'quote_store_unavailable'),
])
def test_quote_error_preserves_contract_without_internal_diagnostic(
    error: Exception, status: int, code: str,
) -> None:
    with pytest.raises(ApiError) as caught:
        tenant_quote_routes._handle_error(error)
    assert (caught.value.status_code, caught.value.code) == (status, code)
    assert 'private-sentinel' not in caught.value.message
    assert any('а' <= letter.lower() <= 'я' for letter in caught.value.message)


def test_quote_conflicts_explain_different_actions_without_promising_a_write() -> None:
    messages = set()
    for code in ('stale_revision', 'approved_document_required', 'quote_already_approved',
                 'quote_not_ready_for_approval', 'idempotency_conflict'):
        with pytest.raises(ApiError) as caught:
            tenant_quote_routes._handle_error(QuoteConflict(code))
        assert (caught.value.status_code, caught.value.code) == (409, code)
        assert code not in caught.value.message
        messages.add(caught.value.message)
    assert len(messages) == 5


def test_unknown_exception_is_not_swallowed_by_copy_handler() -> None:
    error = TypeError('private-sentinel')
    with pytest.raises(TypeError) as caught:
        tenant_quote_routes._handle_error(error)
    assert caught.value is error


@pytest.mark.parametrize('variant', ['tenant', 'legacy'])
@pytest.mark.parametrize(('code', 'status'), [
    ('incomplete_draft', 409), ('inconsistent_quote_total', 409),
    ('cyrillic_font_missing', 503), ('unknown-private-sentinel', 503),
])
def test_pdf_failure_keeps_original_status_without_returning_pdf(
    monkeypatch: pytest.MonkeyPatch, variant: str, code: str, status: int,
) -> None:
    routes = tenant_quote_routes if variant == 'tenant' else persisted_quote_routes
    render = Mock(side_effect=QuotePDFError(code))
    monkeypatch.setattr(routes, 'render_draft_pdf', render)
    quote = draft()
    store = Mock()
    store.get.return_value = quote
    with pytest.raises(ApiError) as caught:
        if variant == 'tenant':
            monkeypatch.setattr(tenant_quote_routes, '_store', Mock(return_value=store))
            tenant_quote_routes.get_tenant_quote_pdf(uuid4(), quote.quote_id, Mock())
        else:
            persisted_quote_routes.quote_pdf(quote.quote_id, store)
    assert (caught.value.status_code, caught.value.code) == (status, code)
    assert code not in caught.value.message
    assert any('а' <= letter.lower() <= 'я' for letter in caught.value.message)
    render.assert_called_once_with(quote)
