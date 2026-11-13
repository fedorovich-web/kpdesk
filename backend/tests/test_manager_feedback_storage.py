"""Offline storage contract; database writes are exercised only by vetted A45."""
from typing import cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import Connection
from test_document_alternatives import saved
from test_manager_feedback_bundle import bundle

from kpdesk.matching.feedback_bundle import FeedbackExportRequest
from kpdesk.matching.feedback_storage import TrainingFeedbackStore, decision_digest
from kpdesk.quotes.storage import QuoteConflict, QuoteNotFound


def request() -> FeedbackExportRequest:
    quote, _ = saved()
    return FeedbackExportRequest.model_validate({
        'expected_revision': quote.revision, 'source_snapshot_sha256': quote.snapshot_sha256,
        'reviewed': True, 'training_consent': True, 'decisions': [
            {'line_number': 1, 'relevant_sku': 'FIRST', 'rejected_skus': ['SECOND', 'THIRD'], 'no_match': False}],
    })


def test_retry_digest_ignores_order_but_preserves_all_explicit_decisions() -> None:
    body = request()
    reversed_body = body.model_copy(update={'decisions': (
        body.decisions[0].model_copy(update={'rejected_skus': ('THIRD', 'SECOND')}),)})
    assert decision_digest(body) == decision_digest(reversed_body)
    changed = body.model_copy(update={'training_consent': False})
    assert decision_digest(body) != decision_digest(changed)


def test_store_rechecks_owner_before_and_after_database_result(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = Mock(spec=Connection)
    calls = []
    monkeypatch.setattr('kpdesk.matching.feedback_storage.bind_authenticated_tenant',
                        lambda *a, **kw: calls.append(kw['operation']))
    feedback = bundle()
    connection.scalar.return_value = {'feedback_id': str(uuid4()), 'quote_id': str(feedback.quote_id),
        'revision': feedback.revision, 'reviewer_id': str(feedback.reviewer_id),
        'created_at': feedback.reviewed_at.isoformat(), 'withdrawn_at': None,
        'event_count': len(feedback.events), 'curation_status': 'pending_review'}
    store = TrainingFeedbackStore(cast(Connection, connection), feedback.organization_id, 'cookie', 'csrf')
    result = store.save(feedback, request(), 'retry-key')
    assert result.reviewer_id == feedback.reviewer_id
    assert calls == ['manage', 'manage']
    assert connection.scalar.call_args.args[0].__str__().startswith('SELECT public.kpdesk_training_feedback_save')


@pytest.mark.parametrize('error', ['feedback_key_conflict', 'stale_revision', 'feedback_storage_limit', 'feedback_withdrawn'])
def test_store_conflicts_do_not_become_empty_success(monkeypatch: pytest.MonkeyPatch, error: str) -> None:
    monkeypatch.setattr('kpdesk.matching.feedback_storage.bind_authenticated_tenant', lambda *a, **kw: None)
    connection = Mock(spec=Connection)
    connection.scalar.return_value = {'error': error}
    feedback = bundle()
    with pytest.raises(QuoteConflict, match=error):
        TrainingFeedbackStore(cast(Connection, connection), feedback.organization_id, 'cookie', 'csrf').get(
            feedback.quote_id, uuid4())


def test_missing_record_is_not_downloaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('kpdesk.matching.feedback_storage.bind_authenticated_tenant', lambda *a, **kw: None)
    connection = Mock(spec=Connection)
    connection.scalar.return_value = None
    with pytest.raises(QuoteNotFound):
        TrainingFeedbackStore(cast(Connection, connection), uuid4(), 'cookie', 'csrf').get(uuid4(), uuid4())


def test_export_cannot_return_if_role_was_revoked_after_read(monkeypatch: pytest.MonkeyPatch) -> None:
    from kpdesk.db.tenant_access import TenantAccessDenied

    calls = 0

    def authorize(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise TenantAccessDenied()

    monkeypatch.setattr('kpdesk.matching.feedback_storage.bind_authenticated_tenant', authorize)
    feedback = bundle()
    connection = Mock(spec=Connection)
    connection.scalar.return_value = feedback.model_dump(mode='json')
    with pytest.raises(TenantAccessDenied):
        TrainingFeedbackStore(cast(Connection, connection), feedback.organization_id, 'cookie', 'csrf').get(
            feedback.quote_id, uuid4())
