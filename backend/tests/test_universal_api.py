"""Universal API boundaries and provider-stage accounting; no DB/network."""
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine

from kpdesk.api import universal_routes as routes
from kpdesk.api.errors import ApiError
from kpdesk.db.llm_budget import InferenceQuotaExceeded
from kpdesk.llm.client import LLMError
from kpdesk.llm.settings import LLMSettings


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',[False,True])
async def test_each_followup_finishes_then_reserves_and_failure_is_completed(monkeypatch: pytest.MonkeyPatch,failure: bool) -> None:
    reserve,finish=Mock(side_effect=lambda *a,**k:uuid4()),Mock()
    monkeypatch.setattr(routes,'reserve_inference',reserve)
    monkeypatch.setattr(routes,'finish_inference',finish)
    monkeypatch.setattr(routes,'operation_budget',lambda *a,**k:Mock())
    async def run(before):  # type: ignore[no-untyped-def]
        await before('next-stage',True)
        assert finish.call_count==1 and reserve.call_count==2
        if failure:raise LLMError('unverified_narrative')
        return 'accepted'
    settings=LLMSettings(api_key='offline',data_mode='real_catalogs')
    arguments=(Mock(spec=Engine),uuid4(),'cookie','csrf',str(uuid4()),settings,Mock(),'test','source',run)
    if failure:
        with pytest.raises(ApiError) as caught:await routes._provider_operation(*arguments)
        assert caught.value.status_code==422
    else:
        assert await routes._provider_operation(*arguments)=='accepted'
    assert finish.call_count==2
    assert finish.call_args.kwargs['succeeded'] is not failure


@pytest.mark.asyncio
async def test_followup_quota_failure_does_not_leave_an_inflight_reservation(monkeypatch: pytest.MonkeyPatch) -> None:
    request_id=uuid4()
    reserve=Mock(side_effect=[request_id,InferenceQuotaExceeded('user_quota')])
    finish=Mock()
    monkeypatch.setattr(routes,'reserve_inference',reserve)
    monkeypatch.setattr(routes,'finish_inference',finish)
    monkeypatch.setattr(routes,'operation_budget',lambda *a,**k:Mock())
    async def run(before):  # type: ignore[no-untyped-def]
        await before('next-stage',True)
    with pytest.raises(ApiError) as caught:
        await routes._provider_operation(Mock(spec=Engine),uuid4(),'cookie','csrf',str(uuid4()),
            LLMSettings(api_key='offline',data_mode='real_catalogs'),Mock(),'test','source',run)
    assert caught.value.status_code==429
    assert finish.call_count==1 and finish.call_args.kwargs['request_id']==request_id


def test_client_prices_and_approval_flags_are_not_accepted_in_quote_request() -> None:
    payload={'catalog_version_id':str(uuid4()),'request':{'lines':[{
        'description':'Насос','source_quote':'Насос 2 шт','quantity':'2','sale_unit':'шт'}]},
        'selections':[{'line_number':1,'sku':'P'}],'issuer_name':'Поставщик'}
    forged_fields: tuple[dict[str,object],...]=({'total_rub':'1.00'},{'approved':True},{'lines':[]})
    for forged in forged_fields:
        with pytest.raises(ValidationError):routes.QuoteBody.model_validate({**payload,**forged})


def test_training_feedback_requires_real_booleans() -> None:
    with pytest.raises(ValidationError):
        routes.FeedbackBody(expected_revision=1,source_snapshot_sha256='a'*64,
            reviewed='true',training_consent=True,decisions=[])  # type: ignore[arg-type]


def test_training_feedback_without_positive_or_negative_decision_is_rejected() -> None:
    with pytest.raises(ValidationError):
        routes.FeedbackDecision(line_number=1,relevant_sku=None,negative_skus=[])



def test_corrupted_stored_snapshots_fail_closed_as_503_not_422(monkeypatch: pytest.MonkeyPatch) -> None:
    from decimal import Decimal
    from types import SimpleNamespace

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

    org_id = uuid4()
    actor = SimpleNamespace(user_id=uuid4())
    cat = CatalogSnapshot(version_id=uuid4(), source_sha256='a' * 64, products=(
        UniversalProduct(sku='P1', name='Насос', sale_unit='шт', price_rub=Decimal('100.00')),
    ))
    req = UniversalRequest(lines=(RequestLine(
        description='Насос', source_quote='Насос 2 шт', quantity=Decimal(2), sale_unit='шт',
    ),))
    valid_quote = build_quote(cat, req, (Selection(line_number=1, sku='P1'),),
                              QuoteContext(issuer_name='Поставщик'), uuid4(), 1)
    tampered_quote = valid_quote.model_dump(mode='json')
    tampered_quote['lines'][0]['sale_quantity'] = '999'
    tampered_quote['snapshot_sha256'] = snapshot_digest(tampered_quote)

    monkeypatch.setattr(UniversalStore, 'authorize', lambda self, **kwargs: actor)
    conn_quote = Mock()
    conn_quote.execute.return_value.mappings.return_value.one_or_none.return_value = {
        'quote_id': valid_quote.quote_id, 'revision': valid_quote.revision,
        'catalog_version_id': cat.version_id,
        'payload': tampered_quote, 'snapshot_sha256': tampered_quote['snapshot_sha256'],
    }
    store_quote = UniversalStore(conn_quote, org_id, 'cookie', None)
    with pytest.raises(ApiError) as exc_quote, routes._errors():
        store_quote.quote(valid_quote.quote_id)
    assert exc_quote.value.status_code == 503
    assert exc_quote.value.code == 'tenant_store_unavailable'

    conn_cat = Mock()
    conn_cat.execute.return_value.mappings.return_value.one_or_none.return_value = {
        'id': cat.version_id,
        'payload': {'broken': True}, 'source_sha256': 'a' * 64,
    }
    store_cat = UniversalStore(conn_cat, org_id, 'cookie', None)
    with pytest.raises(ApiError) as exc_cat, routes._errors():
        store_cat.catalog(cat.version_id)
    assert exc_cat.value.status_code == 503
    assert exc_cat.value.code == 'tenant_store_unavailable'


def test_cross_actor_idempotency_replay_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from kpdesk.universal.storage import UniversalConflict, UniversalStore

    owner_id = uuid4()
    editor_id = uuid4()
    monkeypatch.setattr(UniversalStore, 'authorize', lambda self, **kwargs: SimpleNamespace(user_id=editor_id))
    monkeypatch.setattr(UniversalStore, 'lock', lambda self: SimpleNamespace(user_id=editor_id))

    conn = Mock()
    conn.execute.return_value.mappings.return_value.one_or_none.return_value = {
        'id': uuid4(),
        'payload': {'id': str(uuid4()), 'reviewer_id': str(owner_id)},
        'request_sha256': 'f' * 64,
        'withdrawn_at': None,
        'created_by': owner_id,
    }
    store = UniversalStore(conn, uuid4(), 'cookie', 'csrf')
    with pytest.raises(UniversalConflict, match='idempotency_conflict'):
        store.feedback_replay(uuid4(), 'f' * 64)

    conn.execute.return_value.mappings.return_value.one_or_none.return_value = {
        'quote_id': uuid4(), 'revision': 1, 'request_sha256': 'f' * 64, 'created_by': owner_id,
    }
    with pytest.raises(UniversalConflict, match='idempotency_conflict'):
        store.quote_replay(uuid4(), 'f' * 64)


@pytest.mark.asyncio
async def test_viewer_cannot_read_or_export_universal_feedback_and_routes_set_no_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import contextmanager
    from types import SimpleNamespace

    import httpx

    from kpdesk.api.tenant_session_routes import get_unreleased_catalog_engine
    from kpdesk.db.tenant_access import TenantAccessDenied
    from kpdesk.main import create_app
    from kpdesk.universal.storage import UniversalStore

    monkeypatch.setattr('kpdesk.universal.storage.bind_authenticated_tenant',
                        lambda *a, **k: SimpleNamespace(identity=SimpleNamespace(user_id=uuid4())))
    monkeypatch.setattr('kpdesk.universal.storage.universal_available', lambda conn: True)
    conn = Mock()
    conn.scalar.return_value = False
    store = UniversalStore(conn, uuid4(), 'cookie', None)
    with pytest.raises(TenantAccessDenied):
        store.export_feedback()
    with pytest.raises(TenantAccessDenied):
        store.list_feedback(uuid4())

    app = create_app(testing=True)
    app.include_router(routes.router)
    app.dependency_overrides[get_unreleased_catalog_engine] = lambda: Mock(spec=Engine)

    @contextmanager
    def fake_store(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        mock_store = Mock()
        mock_store.export_feedback.return_value = []
        yield mock_store

    monkeypatch.setattr(routes, '_store', fake_store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url='https://testserver',
    ) as client:
        res = await client.get(f'/api/session/organizations/{uuid4()}/universal/feedback/export')
    assert res.status_code == 200
    assert res.headers.get('cache-control') == 'no-store'



def test_universal_feedback_export_cannot_bypass_typed_trainer_contracts(tmp_path: object) -> None:
    import json
    from pathlib import Path

    from pydantic import ValidationError

    from kpdesk.matching.cli import load_dataset
    from kpdesk.matching.feedback_bundle import FeedbackBundle

    universal_export = {
        'format': 'kpdesk-universal-feedback-v1',
        'training_activated': False,
        'items': [{
            'id': str(uuid4()),
            'quote_id': str(uuid4()),
            'revision': 1,
            'catalog_version_id': str(uuid4()),
            'source_sha256': 'a' * 64,
            'source_request': {'lines': [{
                'description': 'Насос', 'source_quote': 'Насос 2 шт',
                'quantity': '2', 'sale_unit': 'шт', 'requirements': [],
            }]},
            'reviewer_id': str(uuid4()),
            'curation_status': 'pending_review',
            'training_activated': False,
            'reviewed': True,
            'training_consent': True,
            'decisions': [{'line_number': 1, 'relevant_sku': 'P1', 'negative_skus': [], 'no_match_asserted': False}],
        }],
    }
    raw = json.dumps(universal_export, ensure_ascii=False)
    with pytest.raises(ValidationError):
        FeedbackBundle.model_validate_json(raw)

    export_file = Path(str(tmp_path)) / 'kpdesk-universal-feedback.json'
    export_file.write_text(raw, encoding='utf-8')
    with pytest.raises(ValueError, match='Dataset provenance is required'):
        load_dataset(export_file)
