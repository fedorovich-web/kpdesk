"""HTTP owner boundary for durable feedback, no real DB/network."""
from unittest.mock import Mock

import pytest
from test_manager_feedback_api import ORG, Boundary
from test_manager_feedback_api import (
    boundary as boundary,  # noqa: PLC0414 -- pytest fixture re-export
)

from kpdesk.matching.feedback_storage import FeedbackSummary, TrainingFeedbackStore


@pytest.fixture(autouse=True)
def no_saved_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(TrainingFeedbackStore, 'retry', Mock(return_value=None), raising=False)


def test_lost_save_retry_returns_original_even_after_head_advanced(boundary: Boundary, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime
    from uuid import uuid4

    client, state, get = boundary
    quote = state['quote']
    summary = FeedbackSummary(feedback_id=uuid4(), quote_id=quote.quote_id, revision=quote.revision,
                              reviewer_id=uuid4(), created_at=datetime.now(UTC), withdrawn_at=None, event_count=2)
    monkeypatch.setattr(TrainingFeedbackStore, 'retry', Mock(return_value=summary))
    # The saved request refers to revision1; current getter now returns revision2.
    from dataclasses import replace

    get.return_value = replace(quote, revision=quote.revision + 1)
    response = client.post(f'/api/session/organizations/{ORG}/quotes/{quote.quote_id}/training-feedback/history',
                          headers={'Idempotency-Key': 'same-key'}, json={
        'expected_revision': quote.revision, 'source_snapshot_sha256': quote.snapshot_sha256,
        'reviewed': True, 'training_consent': True, 'decisions': [
            {'line_number': 1, 'relevant_sku': 'FIRST', 'rejected_skus': ['SECOND'], 'no_match': False}]})
    assert response.status_code == 200
    assert response.json()['feedback_id'] == str(summary.feedback_id)
    get.assert_not_called()


def test_save_requires_key_and_validates_server_snapshot(boundary: Boundary, monkeypatch: pytest.MonkeyPatch) -> None:
    client, state, _ = boundary
    quote = state['quote']
    save = Mock(side_effect=RuntimeError('unprovisioned'))
    monkeypatch.setattr(TrainingFeedbackStore, 'save', save)
    url = f'/api/session/organizations/{ORG}/quotes/{quote.quote_id}/training-feedback/history'
    body = {'expected_revision': quote.revision, 'source_snapshot_sha256': quote.snapshot_sha256,
            'reviewed': True, 'training_consent': True, 'decisions': [
                {'line_number': 1, 'relevant_sku': 'FIRST', 'rejected_skus': ['SECOND'], 'no_match': False}]}
    assert client.post(url, json=body).status_code == 422
    assert client.post(url, json={**body, 'expected_revision': 99},
                       headers={'Idempotency-Key': 'key'}).status_code == 409
    assert client.post(url, json=body, headers={'Idempotency-Key': 'key'}).status_code == 503
    assert save.call_count == 1
    bundle = save.call_args.args[0]
    assert str(bundle.organization_id) == str(ORG)
    assert all(p.price_rub == 0 for p in bundle.catalog)


@pytest.mark.parametrize('method,suffix', [('get', ''), ('get', '/00000000-0000-4000-8000-000000000001'),
                                        ('post', '/00000000-0000-4000-8000-000000000001/withdraw')])
def test_history_and_saved_export_remain_owner_only(boundary: Boundary, method: str, suffix: str) -> None:
    client, state, _ = boundary
    quote = state['quote']
    state['owner'] = False
    url = f'/api/session/organizations/{ORG}/quotes/{quote.quote_id}/training-feedback/history{suffix}'
    assert client.request(method, url).status_code == 403


def test_late_owner_revocation_prevents_persisting_labels(boundary: Boundary, monkeypatch: pytest.MonkeyPatch) -> None:
    client, state, _ = boundary
    quote = state['quote']
    state['late_revoke'] = True
    save = Mock()
    monkeypatch.setattr(TrainingFeedbackStore, 'save', save)
    response = client.post(f'/api/session/organizations/{ORG}/quotes/{quote.quote_id}/training-feedback/history',
                          headers={'Idempotency-Key': 'key'}, json={
        'expected_revision': quote.revision, 'source_snapshot_sha256': quote.snapshot_sha256,
        'reviewed': True, 'training_consent': True, 'decisions': [
            {'line_number': 1, 'relevant_sku': 'FIRST', 'rejected_skus': ['SECOND'], 'no_match': False}]})
    assert response.status_code == 403
    save.assert_not_called()
