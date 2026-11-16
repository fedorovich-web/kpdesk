"""Offline adapter boundaries: no database, provider or environment access."""
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any
from unittest.mock import Mock

import pytest
from sqlalchemy.exc import SQLAlchemyError


def storage() -> Any:
    from pathlib import Path
    assert (Path(__file__).parents[1] / 'src/kpdesk/yandex/storage.py').exists()
    return import_module('kpdesk.yandex.storage')


def issue_args() -> Any:
    return {'state_hash': 'a' * 64, 'browser_hash': 'b' * 64,
                'verifier_cipher': 'x' * 120, 'mode': 'login', 'next_path': '/app', 'peer': '127.0.0.1'}


def complete_args() -> Any:
    return {'state_hash': 'a' * 64, 'claim_hash': 'c' * 64, 'client_id': 'client', 'subject': '42',
                'email': 'person@example.com', 'session_hash': 'd' * 64, 'csrf_hash': 'e' * 64,
                'expires_at': datetime.now(UTC) + timedelta(days=7)}


@pytest.mark.parametrize('method,args,result', [
    ('issue', issue_args(), {}),
    ('claim', {'state_hash': 'a' * 64, 'browser_hash': 'b' * 64, 'claim_hash': 'c' * 64},
     {'verifier_cipher': 'cipher', 'mode': 'login', 'next_path': '/app', 'expires_at': 'future'}),
    ('complete', complete_args(), {'user_id': '123', 'created': True}),
])
def test_gateway_leaves_transaction_to_caller(method: Any, args: Any, result: Any) -> None:
    module = storage()
    conn = Mock()
    conn.scalar.return_value = result
    output = getattr(module.YandexStore(conn), method)(**args)
    assert output == (None if method == 'issue' else result)
    assert [call[0] for call in conn.method_calls] == ['scalar']
    sql, params = conn.scalar.call_args.args
    assert str(sql).startswith('SELECT public.kpdesk_yandex_')
    expected = ({'session_hash': None, 'csrf_hash': None} | args) if method == 'issue' else args
    assert params == expected


@pytest.mark.parametrize('error', ['flow_invalid', 'link_required', 'account_disabled',
                                  'session_invalid', 'identity_conflict', 'rate_limited'])
def test_domain_error_is_safe_and_caller_must_commit_consumption(error: Any) -> None:
    module = storage()
    conn = Mock()
    conn.scalar.return_value = {'error': error}
    with pytest.raises(module.YandexStorageError) as caught:
        module.YandexStore(conn).complete(**complete_args())
    assert caught.value.code == error
    conn.commit.assert_not_called()
    conn.rollback.assert_not_called()


@pytest.mark.parametrize('method', ['issue', 'claim', 'complete'])
def test_sql_failure_propagates_for_caller_rollback(method: Any) -> None:
    module = storage()
    conn = Mock()
    conn.scalar.side_effect = SQLAlchemyError('secret SQL with tokens')
    args = {'issue': issue_args(), 'complete': complete_args(), 'claim': {
        'state_hash': 'a' * 64, 'browser_hash': 'b' * 64, 'claim_hash': 'c' * 64}}[method]
    with pytest.raises(SQLAlchemyError):
        getattr(module.YandexStore(conn), method)(**args)
    conn.commit.assert_not_called()
    conn.rollback.assert_not_called()


@pytest.mark.parametrize('changes', [
    {'state_hash': 'A' * 64}, {'browser_hash': None}, {'peer': 'x' * 65},
    {'mode': 'admin'}, {'next_path': '//evil.example'}, {'verifier_cipher': 'x' * 4097},
    {'mode': 'link'}, {'session_hash': 'd' * 64}, {'csrf_hash': 'e' * 64},
    {'next_path': ['/app']}, {'peer': True},
])
def test_invalid_issue_is_rejected_before_database(changes: Any) -> None:
    module = storage()
    conn = Mock()
    with pytest.raises(module.YandexStorageError, match='invalid_request'):
        module.YandexStore(conn).issue(**(issue_args() | changes))
    assert conn.method_calls == []


@pytest.mark.parametrize('changes', [
    {'client_id': ''}, {'subject': 'x' * 256}, {'session_hash': 'bad'},
    {'email': 'USER@example.com'}, {'email': 'not email'}, {'email': 1},
    {'expires_at': datetime.now(UTC).replace(tzinfo=None)},
    {'expires_at': datetime.now(UTC) + timedelta(days=8)},
    {'expires_at': datetime.now(UTC) - timedelta(seconds=1)},
])
def test_invalid_completion_is_rejected_before_database(changes: Any) -> None:
    module = storage()
    conn = Mock()
    with pytest.raises(module.YandexStorageError, match='invalid_request'):
        module.YandexStore(conn).complete(**(complete_args() | changes))
    assert conn.method_calls == []


def test_identity_is_read_only_and_own_session_bound() -> None:
    module = storage()
    conn = Mock()
    conn.scalar.return_value = True
    assert module.YandexStore(conn).identity(session_hash='a' * 64, client_id='client')
    conn.commit.assert_not_called()
    assert conn.scalar.call_args.args[1] == {'session_hash': 'a' * 64, 'client_id': 'client'}


def test_unrecognized_database_message_is_never_exposed() -> None:
    module = storage()
    conn = Mock()
    conn.scalar.return_value = {'error': 'secret raw failure'}
    with pytest.raises(module.YandexStorageError) as caught:
        module.YandexStore(conn).issue(**issue_args())
    assert caught.value.code == 'storage_unavailable'
