"""Stored snapshots must agree with relational identities, not just their own hash."""
from decimal import Decimal
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
    RequestLine,
    Selection,
    UniversalProduct,
    UniversalRequest,
    snapshot_digest,
)
from kpdesk.universal.storage import UniversalStore


def catalog() -> CatalogSnapshot:
    return CatalogSnapshot(version_id=uuid4(), source_sha256='a' * 64, products=(
        UniversalProduct(sku='P', name='Насос', sale_unit='шт', price_rub=Decimal(100)),))


@pytest.mark.parametrize('field', ['quote_id', 'revision', 'catalog_version_id'])
def test_self_consistent_quote_with_wrong_relational_identity_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, field: str,
) -> None:
    cat = catalog()
    request = UniversalRequest(lines=(RequestLine(description='Насос', source_quote='Насос 2 шт',
                                                   quantity=Decimal(2), sale_unit='шт'),))
    quote = build_quote(cat, request, (Selection(line_number=1, sku='P'),),
                        QuoteContext(issuer_name='Поставщик'), uuid4(), 1)
    payload = quote.model_dump(mode='json')
    payload[field] = 2 if field == 'revision' else str(uuid4())
    payload['snapshot_sha256'] = snapshot_digest(payload)
    conn = Mock()
    conn.execute.return_value.mappings.return_value.one_or_none.return_value = {
        'quote_id': quote.quote_id, 'revision': quote.revision, 'catalog_version_id': cat.version_id,
        'payload': payload, 'snapshot_sha256': payload['snapshot_sha256'],
    }
    monkeypatch.setattr(UniversalStore, 'authorize', lambda self, **kwargs: SimpleNamespace(user_id=uuid4()))
    store = UniversalStore(conn, uuid4(), 'synthetic', None)
    with pytest.raises(ApiError) as error, routes._errors():
        store.quote(quote.quote_id, quote.revision)
    assert (error.value.status_code, error.value.code) == (503, 'tenant_store_unavailable')


@pytest.mark.parametrize('replay', [False, True])
def test_catalog_identity_mismatch_is_unavailable_on_read_and_replay(
    monkeypatch: pytest.MonkeyPatch, replay: bool,
) -> None:
    from kpdesk.universal.storage import digest

    cat, actor_id = catalog(), uuid4()
    payload = cat.model_dump(mode='json')
    payload['version_id'] = str(uuid4())
    conn = Mock()
    conn.execute.return_value.mappings.return_value.one_or_none.return_value = {
        'id': cat.version_id, 'payload': payload, 'source_sha256': cat.source_sha256,
        'created_by': actor_id, 'request_sha256': digest({'source_sha256': cat.source_sha256}),
    }
    monkeypatch.setattr(UniversalStore, 'authorize', lambda self, **kwargs: SimpleNamespace(user_id=actor_id))
    monkeypatch.setattr(UniversalStore, 'lock', lambda self: SimpleNamespace(user_id=actor_id))
    store = UniversalStore(conn, uuid4(), 'synthetic', None)
    with pytest.raises(ApiError) as error, routes._errors():
        if replay:
            store.publish(cat, uuid4())
        else:
            store.catalog(cat.version_id)
    assert (error.value.status_code, error.value.code) == (503, 'tenant_store_unavailable')
