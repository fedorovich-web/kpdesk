"""Bounded races/quotas on the vetted A45 clone; never a standalone test runner."""
import hashlib
import json
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from queue import Queue
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from kpdesk.api.tenant_session_routes import SESSION_COOKIE
from kpdesk.db.auth_sessions import AuthSessionRepository, IssuedSession, hash_password
from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone
from kpdesk.db.tenant_access import TenantAccessDenied
from kpdesk.matching.feedback_bundle import FeedbackBundle, FeedbackExportRequest
from kpdesk.matching.feedback_storage import TrainingFeedbackStore
from kpdesk.quotes.storage import QuoteConflict


def verify_feedback_storage_races(
    owner: Engine, tenant: Engine, auth: Engine, org: UUID, session: IssuedSession,
    factory: Callable[[str], FastAPI], output: Path,
) -> None:
    with owner.connect() as connection:
        assert is_live_accounts_http_clone(connection)
        record = connection.execute(text('''SELECT f.* FROM public.training_feedback_records f
            WHERE organization_id=:org AND reviewer_id=:actor
            AND NOT EXISTS(SELECT 1 FROM public.training_feedback_withdrawals w
              WHERE w.organization_id=f.organization_id AND w.feedback_id=f.id)
            ORDER BY created_at ASC LIMIT 1'''), {'org': org, 'actor': session.user_id}).mappings().one()
    bundle = FeedbackBundle.model_validate(record['bundle'])
    quote_id = bundle.quote_id
    groups: dict[str, list[Any]] = {}
    for event in bundle.events:
        groups.setdefault(event.query_id, []).append(event)
    decisions = []
    for number in range(1, 21):
        identity = f'{org}:{quote_id}:{bundle.revision}:{bundle.snapshot_sha256}:{number}'
        group = groups.get('q-' + hashlib.sha256(identity.encode()).hexdigest()[:40])
        if group:
            decisions.append({'line_number': number, 'relevant_sku': next((e.sku for e in group if e.verdict == 'relevant'), None),
                              'no_match': any(e.verdict == 'no_match' for e in group),
                              'rejected_skus': [e.sku for e in group if e.verdict == 'irrelevant']})
    body = FeedbackExportRequest.model_validate({'expected_revision': bundle.revision,
        'source_snapshot_sha256': bundle.snapshot_sha256, 'reviewed': True, 'training_consent': True, 'decisions': decisions})
    base = f'/api/session/organizations/{org}/quotes/{quote_id}'
    headers = {'Origin': 'https://testserver', 'X-CSRF-Token': session.csrf_token}

    def waiting(pid: int) -> None:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            with owner.connect() as connection:
                if connection.scalar(text('SELECT cardinality(pg_blocking_pids(:pid))>0'), {'pid': pid}):
                    return
            time.sleep(.02)
        raise AssertionError('Expected a real PostgreSQL lock wait')

    def worker(queue: Queue[int], action: str, identity: IssuedSession, feedback_id: UUID) -> object:
        with tenant.begin() as connection:
            queue.put(connection.scalar(text('SELECT pg_backend_pid()')))
            store = TrainingFeedbackStore(connection, org, identity.session_token, identity.csrf_token)
            if action == 'retry':
                return store.retry(quote_id, body, record['idempotency_key'])
            if action == 'get':
                return store.get(quote_id, feedback_id)
            return store.withdraw(quote_id, feedback_id)

    # Distinct real owner/session, not an application-supplied actor header.
    actor = uuid4()
    password = 'Synthetic-Feedback-Races-Only-2026!'
    email = f'feedback-{actor.hex}@example.invalid'
    with owner.begin() as connection:
        assert is_live_accounts_http_clone(connection)
        connection.execute(text("INSERT INTO public.users(id,email,password_hash,status,email_verified_at) VALUES(:id,:email,:password,'active',clock_timestamp())"),
                           {'id': actor, 'email': email, 'password': hash_password(password)})
        connection.execute(text("INSERT INTO public.organization_memberships(organization_id,user_id,role,status) VALUES(:org,:id,'owner','active')"),
                           {'org': org, 'id': actor})

    def login() -> IssuedSession:
        with auth.begin() as connection:
            return AuthSessionRepository(connection).login(email, password, peer='127.0.0.4')

    other = login()
    with tenant.begin() as connection:
        try:
            TrainingFeedbackStore(connection, org, other.session_token, other.csrf_token).retry(quote_id, body, record['idempotency_key'])
            raise AssertionError('Different owner reused another owner key')
        except QuoteConflict as error:
            assert str(error) == 'feedback_key_conflict'

    with ThreadPoolExecutor(max_workers=2) as executor:
        for scenario in ('expiry', 'role_revoked'):
            victim = other if scenario == 'expiry' else login()
            queue: Queue[int] = Queue()
            with owner.connect() as blocker:
                transaction = blocker.begin()
                blocker.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('kpdesk:feedback:'||:org,0))"), {'org': str(org)})
                pending = executor.submit(worker, queue, 'retry', victim, record['id'])
                waiting(queue.get(timeout=8))
                with owner.begin() as connection:
                    assert is_live_accounts_http_clone(connection)
                    if scenario == 'expiry':
                        connection.execute(text('UPDATE public.auth_sessions SET expires_at=created_at+interval \'1 millisecond\' WHERE token_sha256=:sha'),
                                           {'sha': hashlib.sha256(victim.session_token.encode()).hexdigest()})
                    else:
                        connection.execute(text("UPDATE public.organization_memberships SET role='editor' WHERE organization_id=:org AND user_id=:actor"),
                                           {'org': org, 'actor': actor})
                transaction.commit()
                try:
                    pending.result(timeout=8)
                    raise AssertionError('Authority changed during lock wait but data was returned')
                except TenantAccessDenied:
                    pass

        active: list[UUID]
        with owner.connect() as connection:
            active = list(connection.scalars(text('''SELECT f.id FROM public.training_feedback_records f
                WHERE organization_id=:org AND NOT EXISTS(SELECT 1 FROM public.training_feedback_withdrawals w
                WHERE w.organization_id=f.organization_id AND w.feedback_id=f.id)
                ORDER BY created_at DESC LIMIT 2'''), {'org': org}))
        assert len(active) == 2
        for first, second, feedback_id in (('get', 'withdraw', active[0]), ('withdraw', 'get', active[1])):
            queue = Queue()
            with tenant.connect() as connection:
                transaction = connection.begin()
                store = TrainingFeedbackStore(connection, org, session.session_token, session.csrf_token)
                if first == 'get':
                    assert store.get(quote_id, feedback_id).quote_id == quote_id
                else:
                    assert store.withdraw(quote_id, feedback_id).withdrawn_at
                pending = executor.submit(worker, queue, second, session, feedback_id)
                waiting(queue.get(timeout=8))
                assert not pending.done()
                transaction.commit()
                if second == 'get':
                    try:
                        pending.result(timeout=8)
                        raise AssertionError('Withdrawal completed before read but bundle was exported')
                    except QuoteConflict as error:
                        assert str(error) == 'feedback_withdrawn'
                else:
                    assert pending.result(timeout=8) is not None

    # Byte quota fixture exists only inside an owner savepoint and is rolled back.
    # Padding is intentionally a storage-boundary fixture, not exportable labels.
    statement = text('SELECT public.kpdesk_training_feedback_save(:org,:bundle,:digest,:key)').bindparams(bindparam('bundle', type_=JSONB))
    with owner.begin() as connection:
        assert is_live_accounts_http_clone(connection)
        savepoint = connection.begin_nested()
        try:
            connection.execute(text("SELECT set_config('kpdesk.session_sha256',:sha,true)"),
                               {'sha': hashlib.sha256(session.session_token.encode()).hexdigest()})
            connection.execute(text('''INSERT INTO public.training_feedback_records
              (id,organization_id,quote_id,revision,snapshot_sha256,reviewer_id,idempotency_key,request_sha256,bundle)
              SELECT gen_random_uuid(),organization_id,quote_id,revision,snapshot_sha256,reviewer_id,
                'byte-fixture-'||n::text,request_sha256,bundle||jsonb_build_object('fixture_padding',repeat('x',9*1024*1024))
              FROM public.training_feedback_records CROSS JOIN generate_series(1,12) n
              WHERE organization_id=:org AND id=:id'''), {'org': org, 'id': record['id']})
            result = connection.scalar(statement, {'org': org, 'bundle': bundle.model_dump(mode='json'),
                'digest': record['request_sha256'], 'key': 'beyond-byte-quota'})
            assert result == {'error': 'feedback_storage_limit'}
        finally:
            savepoint.rollback()
        count = connection.scalar(text('SELECT count(*) FROM public.training_feedback_records WHERE organization_id=:org'), {'org': org})
        connection.execute(text('''INSERT INTO public.training_feedback_records
          (id,organization_id,quote_id,revision,snapshot_sha256,reviewer_id,idempotency_key,request_sha256,bundle)
          SELECT gen_random_uuid(),organization_id,quote_id,revision,snapshot_sha256,reviewer_id,
            'count-fixture-'||n::text,request_sha256,bundle
          FROM public.training_feedback_records CROSS JOIN generate_series(1,:amount) n
          WHERE organization_id=:org AND id=:id'''), {'org': org, 'id': record['id'], 'amount': 499-count})

    def save_http() -> int:
        with TestClient(factory('https://testserver'), base_url='https://testserver') as client:
            client.cookies.set(SESSION_COOKIE, session.session_token)
            return int(client.post(base+'/training-feedback/history', json=body.model_dump(mode='json'),
                                   headers={**headers, 'Idempotency-Key': uuid4().hex}).status_code)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures: list[Future[int]] = [executor.submit(save_http) for _ in range(2)]
        assert sorted(f.result(timeout=20) for f in futures) == [200, 409]
    with owner.connect() as connection:
        assert connection.scalar(text('SELECT count(*) FROM public.training_feedback_records WHERE organization_id=:org'), {'org': org}) == 500
        current_catalog = connection.scalar(text('SELECT catalog_version_id FROM public.tenant_catalog_active WHERE organization_id=:org'), {'org': org})

    # A completed save remains replayable after a real API revision advances head.
    with TestClient(factory('https://testserver'), base_url='https://testserver') as client:
        client.cookies.set(SESSION_COOKIE, session.session_token)
        current = client.get(base).json()
        revised = client.post(base+'/revisions', headers={**headers, 'Idempotency-Key': uuid4().hex}, json={
            'expected_revision': current['revision'], 'catalog_version_id': str(current_catalog),
            'extracted_request': current['source_request'],
        })
        assert revised.status_code == 201, revised.text
        assert revised.json()['revision'] > bundle.revision
        replay = client.post(base+'/training-feedback/history', json=body.model_dump(mode='json'),
                             headers={**headers, 'Idempotency-Key': record['idempotency_key']})
        assert replay.status_code == 200, replay.text
        assert replay.json()['feedback_id'] == str(record['id'])
    (output/'clone-storage-races.json').write_text(json.dumps({
        'different_owner_key_rejected': True, 'expiry_and_role_change_after_real_lock_wait': True,
        'withdrawal_download_both_lock_orders': True, 'byte_quota_owner_savepoint_fixture': True,
        'concurrent_count_quota_http_statuses': [200,409], 'final_records': 500,
        'replay_checked_after_real_quote_revision': replay.status_code,
        'provider_calls': 0, 'production_model_changed': False,
    }, indent=2), encoding='utf-8')
