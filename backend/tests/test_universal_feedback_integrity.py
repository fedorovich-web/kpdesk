"""Consent and provenance must survive feedback persistence and replay."""
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest

from kpdesk.api import universal_routes as routes
from kpdesk.api.errors import ApiError
from kpdesk.universal.matching import build_quote
from kpdesk.universal.schema import CatalogSnapshot, QuoteContext, QuoteSnapshot, UniversalRequest
from kpdesk.universal.storage import UniversalConflict, UniversalStore, digest


def saved_feedback() -> tuple[QuoteSnapshot, dict[str, Any]]:
    catalog = CatalogSnapshot.model_validate({'version_id': uuid4(), 'source_sha256': 'a' * 64,
        'products': [{'sku': 'P', 'name': 'Насос', 'price_rub': '100.00', 'sale_unit': 'шт'},
                     {'sku': 'Q', 'name': 'Кабель', 'price_rub': '10.00', 'sale_unit': 'м'}]})
    request = UniversalRequest.model_validate({'lines': [{'description': 'Насос',
        'source_quote': 'Насос 2 шт', 'quantity': '2', 'sale_unit': 'шт'}]})
    quote = build_quote(catalog, request, routes.QuoteBody.model_validate({
        'catalog_version_id': catalog.version_id, 'request': request,
        'selections': [{'line_number': 1, 'sku': 'P'}], 'issuer_name': 'Поставщик',
    }).selections, QuoteContext(issuer_name='Поставщик'), uuid4(), 1)
    assert quote.total_rub == Decimal('200.00')
    body = {'expected_revision': 1, 'source_snapshot_sha256': quote.snapshot_sha256,
            'reviewed': True, 'training_consent': True,
            'decisions': [{'line_number': 1, 'relevant_sku': 'P', 'negative_skus': []}]}
    actor, feedback_id = uuid4(), uuid4()
    payload: dict[str, Any] = {**deepcopy(body), 'id': str(feedback_id), 'quote_id': str(quote.quote_id),
        'revision': 1, 'catalog_version_id': str(quote.catalog_version_id),
        'source_sha256': quote.source_sha256, 'source_request': request.model_dump(mode='json'),
        'reviewer_id': str(actor), 'curation_status': 'pending_review', 'training_activated': False}
    payload['decisions'][0]['no_match_asserted'] = False
    return quote, {'id': feedback_id, 'quote_id': quote.quote_id, 'revision': 1,
                   'created_by': actor, 'payload': payload, 'withdrawn_at': None,
                   'request_sha256': digest({'quote': str(quote.quote_id), 'feedback': body})}


def reader(monkeypatch: pytest.MonkeyPatch, row: dict[str, Any], quote: QuoteSnapshot) -> UniversalStore:
    monkeypatch.setattr(UniversalStore, 'authorize', lambda self, **kw: SimpleNamespace(user_id=row['created_by']))
    monkeypatch.setattr(UniversalStore, 'quote', lambda self, quote_id, revision=None, **kw: quote)
    catalog = CatalogSnapshot.model_validate({'version_id': quote.catalog_version_id,
        'source_sha256': quote.source_sha256,
        'products': [{'sku': 'P', 'name': 'Насос', 'price_rub': '100.00', 'sale_unit': 'шт'},
                     {'sku': 'Q', 'name': 'Кабель', 'price_rub': '10.00', 'sale_unit': 'м'}]})
    monkeypatch.setattr(UniversalStore, 'catalog', lambda *args: catalog)
    conn = Mock()
    result = conn.execute.return_value
    result.mappings.return_value.one_or_none.return_value = row
    result.mappings.return_value.all.return_value = [row]
    result.scalars.return_value = [row['payload']]
    return UniversalStore(conn, uuid4(), 'synthetic-cookie', None)


def read_feedback(store: UniversalStore, route: str, row: dict[str, Any]) -> object:
    if route == 'replay':
        return store.feedback_replay(uuid4(), row['request_sha256'])
    if route == 'list':
        return store.list_feedback(row['quote_id'])
    return store.export_feedback()


@pytest.mark.parametrize('route', ['replay', 'list', 'export'])
@pytest.mark.parametrize('mutation', ['empty', 'consent', 'review', 'activation', 'curation', 'id',
                                     'quote_id', 'revision', 'catalog', 'source', 'request',
                                     'reviewer', 'decisions', 'no_match', 'decision_content'])
def test_corrupt_feedback_cannot_be_replayed_or_exported_as_consented_labels(
    monkeypatch: pytest.MonkeyPatch, route: str, mutation: str,
) -> None:
    quote, row = saved_feedback()
    payload = row['payload']
    if mutation == 'empty':
        row['payload'] = {}
    elif mutation in ('consent', 'review'):
        payload['training_consent' if mutation == 'consent' else 'reviewed'] = False
    elif mutation == 'activation':
        payload['training_activated'] = True
    elif mutation == 'curation':
        payload['curation_status'] = 'independent_gold'
    elif mutation in ('id', 'quote_id'):
        payload[mutation] = str(uuid4())
    elif mutation == 'revision':
        payload['revision'] = payload['expected_revision'] = 2
    elif mutation == 'catalog':
        payload['catalog_version_id'] = str(uuid4())
    elif mutation == 'source':
        payload['source_sha256'] = 'b' * 64
    elif mutation == 'request':
        payload['source_request']['lines'][0]['description'] = 'Подменённая заявка'
    elif mutation == 'reviewer':
        payload['reviewer_id'] = str(uuid4())
    elif mutation == 'decisions':
        payload['decisions'][0]['line_number'] = 2
    elif mutation == 'no_match':
        payload['decisions'][0]['no_match_asserted'] = True
    elif mutation == 'decision_content':
        payload['decisions'][0]['relevant_sku'] = 'UNREVIEWED'
    store = reader(monkeypatch, row, quote)
    with pytest.raises(ApiError) as error, routes._errors():
        read_feedback(store, route, row)
    assert (error.value.status_code, error.value.code) == (503, 'tenant_store_unavailable')


@pytest.mark.parametrize('route', ['replay', 'list', 'export'])
def test_legitimate_pending_review_feedback_preserves_export_shape(
    monkeypatch: pytest.MonkeyPatch, route: str,
) -> None:
    quote, row = saved_feedback()
    store = reader(monkeypatch, row, quote)
    result = read_feedback(store, route, row)
    assert result == (row['payload'] if route == 'replay' else [row['payload']])


def test_withdrawn_feedback_is_never_replayed_even_when_payload_is_corrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    quote, row = saved_feedback()
    row.update(payload={}, withdrawn_at='synthetic-withdrawal')
    store = reader(monkeypatch, row, quote)
    with pytest.raises(UniversalConflict, match='feedback_withdrawn'):
        store.feedback_replay(uuid4(), row['request_sha256'])


@pytest.mark.parametrize('route', ['replay', 'list', 'export'])
def test_partial_negative_review_is_not_a_global_no_match(
    monkeypatch: pytest.MonkeyPatch, route: str,
) -> None:
    quote, row = saved_feedback()
    row['payload']['decisions'] = [{'line_number': 1, 'relevant_sku': None,
                                    'negative_skus': ['P'], 'no_match_asserted': True}]
    body = {name: row['payload'][name] for name in
            ('expected_revision', 'source_snapshot_sha256', 'reviewed', 'training_consent')}
    body['decisions'] = [{'line_number': 1, 'relevant_sku': None, 'negative_skus': ['P']}]
    row['request_sha256'] = digest({'quote': str(quote.quote_id), 'feedback': body})
    with pytest.raises(ApiError) as error, routes._errors():
        read_feedback(reader(monkeypatch, row, quote), route, row)
    assert error.value.status_code == 503
