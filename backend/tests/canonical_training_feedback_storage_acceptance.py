"""Durable feedback HTTP checks on the existing tagged A45 clone only."""
import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient


def verify_feedback_storage(
    manager: TestClient, editor: TestClient, endpoint: str, scope: dict[str, Any],
    decisions: list[dict[str, Any]], owner_headers: dict[str, str], editor_headers: dict[str, str],
    outsider: UUID, organization: UUID, art: Path,
) -> None:
    url = endpoint + '/history'
    body = {**scope, 'reviewed': True, 'training_consent': True, 'decisions': decisions}
    headers = {**owner_headers, 'Idempotency-Key': 'storage-' + uuid4().hex}
    before = manager.get(url, headers=owner_headers)
    assert before.status_code == 200 and before.json()['items'] == []
    assert manager.post(url, json=body, headers=owner_headers).status_code == 422
    assert editor.post(url, json=body, headers={**editor_headers, 'Idempotency-Key': 'editor'}).status_code == 403
    assert manager.post(url, json={**body, 'training_consent': False}, headers=headers).status_code == 422
    assert manager.post(url, json={**body, 'expected_revision': 999}, headers=headers).status_code == 409
    saved = manager.post(url, json=body, headers=headers)
    assert saved.status_code == 200, saved.text
    record = saved.json()
    record_url = url + '/' + record['feedback_id']
    assert manager.post(url, json=body, headers=headers).json() == record
    assert record['curation_status'] == 'pending_review' and record['withdrawn_at'] is None
    listing = manager.get(url, headers=owner_headers)
    assert listing.status_code == 200 and listing.json()['items'] == [record]
    assert listing.headers['cache-control'] == 'no-store'
    for target in (url, record_url):
        assert manager.get(target).status_code == 403  # no CSRF for owner/manage read
        assert editor.get(target, headers=editor_headers).status_code == 403
        assert manager.get(target.replace(str(organization), str(outsider)), headers=owner_headers).status_code == 403
    downloaded = manager.get(record_url, headers=owner_headers)
    assert downloaded.status_code == 200 and all(p['price_rub'] == '0.00' for p in downloaded.json()['catalog'])
    assert 'attachment' in downloaded.headers['content-disposition']
    assert manager.get(url, params={'before': str(uuid4())}, headers=owner_headers).status_code == 409
    # Same valid source, different explicit decision, same key is a conflict.
    all_skus = [p['sku'] for p in downloaded.json()['catalog']]
    altered = [{**d, 'relevant_sku': None, 'no_match': True, 'rejected_skus': all_skus} for d in decisions]
    assert manager.post(url, json={**body, 'decisions': altered}, headers=headers).status_code == 409
    assert editor.post(record_url + '/withdraw', headers=editor_headers).status_code == 403
    assert manager.post(record_url + '/withdraw', headers={'Origin': 'https://testserver'}).status_code == 401
    withdrawn = manager.post(record_url + '/withdraw', headers=owner_headers)
    assert withdrawn.status_code == 200 and withdrawn.json()['withdrawn_at']
    assert manager.post(record_url + '/withdraw', headers=owner_headers).json() == withdrawn.json()
    assert manager.get(record_url, headers=owner_headers).status_code == 409
    assert manager.post(url, json=body, headers=headers).status_code == 409
    assert manager.get(url, headers=owner_headers).json()['items'] == [withdrawn.json()]
    assert manager.post(url, json=body, headers={**owner_headers, 'Idempotency-Key': uuid4().hex}).status_code == 200
    for _ in range(20):
        assert manager.post(url, json=body, headers={**owner_headers, 'Idempotency-Key': uuid4().hex}).status_code == 200
    page_one = manager.get(url, headers=owner_headers).json()
    assert len(page_one['items']) == 20 and page_one['next_cursor']
    page_two = manager.get(url, headers=owner_headers, params={'before': page_one['next_cursor']}).json()
    assert len(page_two['items']) == 2 and page_two['next_cursor'] is None
    assert len({r['feedback_id'] for r in page_one['items'] + page_two['items']}) == 22
    (art / 'clone-storage-http.json').write_text(json.dumps({
        'real_clone_auth_restricted_postgres': True, 'idempotent_save': True,
        'conflicting_key_rejected': True, 'consent_withdrawn_blocks_saved_export': True,
        'history_retained': True, 'fresh_explicit_consent_creates_new_record': True,
        'keyset_history_pages': [20, 2],
        'model_activated': False, 'provider_calls': 0,
    }, indent=2), encoding='utf-8')


def verify_feedback_storage_sql(owner: Any, tenant: Any) -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone
    from kpdesk.db.training_feedback_schema import drop_empty_training_feedback_objects

    with owner.begin() as connection:
        assert is_live_accounts_http_clone(connection)
        for table in ('training_feedback_records', 'training_feedback_withdrawals'):
            assert connection.scalar(text(f"SELECT relforcerowsecurity FROM pg_class WHERE oid='public.{table}'::regclass"))
            before = connection.scalar(text(f'SELECT count(*) FROM public.{table}'))
            assert before > 0
            for operation in ('UPDATE', 'DELETE'):
                savepoint = connection.begin_nested()
                try:
                    statement = f'UPDATE public.{table} SET created_at=clock_timestamp()' if operation == 'UPDATE' else f'DELETE FROM public.{table}'
                    connection.execute(text(statement))
                    raise AssertionError('Immutable feedback audit was modified')
                except SQLAlchemyError:
                    pass
                finally:
                    savepoint.rollback()
                assert connection.scalar(text(f'SELECT count(*) FROM public.{table}')) == before
        try:
            drop_empty_training_feedback_objects(connection)
            raise AssertionError('Populated feedback schema was dropped')
        except RuntimeError as error:
            assert 'business data' in str(error)
    with tenant.begin() as connection:
        for table in ('training_feedback_records', 'training_feedback_withdrawals'):
            for privilege in ('SELECT', 'INSERT', 'UPDATE', 'DELETE'):
                assert connection.scalar(text('SELECT has_table_privilege(current_user,:table,:privilege)'),
                                         {'table': 'public.' + table, 'privilege': privilege}) is False
        assert connection.scalar(text('SELECT public.kpdesk_training_feedback_history(:org,:quote,NULL)'),
                                 {'org': uuid4(), 'quote': uuid4()}) is None
    print('Feedback SQL: FORCE RLS, direct privilege denial, immutable audit, nonempty downgrade refusal: PASS')
