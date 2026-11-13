"""A consistent hash/total cannot replace facts from the immutable source catalog."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from kpdesk.api import universal_routes as routes
from kpdesk.api.errors import ApiError
from kpdesk.universal.matching import build_quote
from kpdesk.universal.schema import (
    CatalogSnapshot,
    QuoteContext,
    QuoteSnapshot,
    UniversalRequest,
    snapshot_digest,
)
from kpdesk.universal.storage import UniversalStore


def saved_quote() -> tuple[CatalogSnapshot, QuoteSnapshot]:
    catalog = CatalogSnapshot.model_validate({'version_id': uuid4(), 'source_sha256': 'a' * 64,
        'products': [{'sku': 'C', 'name': 'Кабель', 'price_rub': '1200.00', 'price_quantity': '100',
                      'sale_unit': 'м', 'brand': 'Источник', 'model': 'C1', 'category': 'Кабели',
                      'specifications': [{'name': 'материал', 'value': 'медь'}]}]})
    request = UniversalRequest.model_validate({'lines': [{'description': 'Кабель',
        'source_quote': 'Кабель 250 см', 'quantity': '250', 'sale_unit': 'см'}]})
    body = routes.QuoteBody.model_validate({'catalog_version_id': catalog.version_id,
        'request': request, 'selections': [{'line_number': 1, 'sku': 'C'}], 'issuer_name': 'Поставщик'})
    quote = build_quote(catalog, request, body.selections, QuoteContext(issuer_name='Поставщик'), uuid4(), 1)
    return catalog, quote


def changed_quote(quote: QuoteSnapshot, field: str) -> QuoteSnapshot:
    payload = deepcopy(quote.model_dump(mode='json'))
    line = payload['lines'][0]
    if field == 'source_sha256':
        payload[field] = 'b' * 64
    elif field == 'price_rub':
        line[field], line['line_total_rub'], payload['total_rub'] = '1.00', '0.03', '0.03'
    elif field == 'price_quantity':
        line[field], line['line_total_rub'], payload['total_rub'] = '1', '3000.00', '3000.00'
    elif field == 'sale_quantity':
        line[field], line['line_total_rub'], payload['total_rub'] = '5', '60.00', '60.00'
    elif field == 'request_quantity':
        payload['source_request']['lines'][0]['quantity'] = None
    elif field == 'sale_unit':
        line[field] = 'кг'
    elif field == 'specifications':
        line[field] = [{'name': 'материал', 'value': 'алюминий', 'unit': None}]
    else:
        line[field] = 'Подменено'
    payload['snapshot_sha256'] = snapshot_digest(payload)
    return QuoteSnapshot.model_validate(payload)


@pytest.mark.parametrize('operation', ['read', 'save'])
@pytest.mark.parametrize('field', ['source_sha256', 'price_rub', 'price_quantity', 'sale_quantity',
                                  'request_quantity', 'sale_unit', 'specifications', 'name',
                                  'brand', 'model', 'category'])
def test_quote_cannot_substitute_catalog_facts_or_requested_quantity(
    monkeypatch: pytest.MonkeyPatch, operation: str, field: str,
) -> None:
    catalog, original = saved_quote()
    quote = changed_quote(original, field)
    conn = Mock()
    row = {'quote_id': quote.quote_id, 'revision': quote.revision, 'catalog_version_id': catalog.version_id,
           'payload': quote.model_dump(mode='json'), 'snapshot_sha256': quote.snapshot_sha256}
    conn.execute.return_value.mappings.return_value.one_or_none.return_value = row
    actor = SimpleNamespace(user_id=uuid4())
    monkeypatch.setattr(UniversalStore, 'authorize', lambda self, **kwargs: actor)
    monkeypatch.setattr(UniversalStore, 'lock', lambda self: actor)
    monkeypatch.setattr(UniversalStore, 'quote_replay', lambda *args: None)
    monkeypatch.setattr(UniversalStore, 'catalog', lambda *args: catalog)
    store = UniversalStore(conn, uuid4(), 'synthetic', None)
    if operation == 'read':
        with pytest.raises(ApiError) as error, routes._errors():
            store.quote(quote.quote_id)
        assert (error.value.status_code, error.value.code) == (503, 'tenant_store_unavailable')
    else:
        with pytest.raises(ValueError):
            store.save_quote(quote, uuid4(), 'f' * 64)
        conn.execute.assert_not_called()


def test_historical_quote_uses_its_own_catalog_and_valid_unit_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
    catalog, quote = saved_quote()
    conn = Mock()
    conn.execute.return_value.mappings.return_value.one_or_none.return_value = {
        'quote_id': quote.quote_id, 'revision': quote.revision, 'catalog_version_id': catalog.version_id,
        'payload': quote.model_dump(mode='json'), 'snapshot_sha256': quote.snapshot_sha256,
    }
    requested: list[object] = []
    def own_catalog(self: UniversalStore, version_id: object = None) -> CatalogSnapshot:
        requested.append(version_id)
        return catalog
    monkeypatch.setattr(UniversalStore, 'authorize', lambda self, **kw: SimpleNamespace(user_id=uuid4()))
    monkeypatch.setattr(UniversalStore, 'catalog', own_catalog)
    assert UniversalStore(conn, uuid4(), 'synthetic', None).quote(quote.quote_id, 1) == quote
    assert requested == [catalog.version_id]
