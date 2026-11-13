"""Bound logical-ledger SQL waits before authentication, including cleanup/seal."""
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, call
from uuid import uuid4

import pytest
from sqlalchemy import Connection, Engine

from kpdesk.db import llm_budget, llm_cache


@pytest.mark.parametrize('operation', ['reserve', 'finish', 'seal'])
def test_logical_sql_has_transaction_local_timeouts(
    monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    connection = MagicMock(spec=Connection)
    connection.scalar.return_value = 'reserved' if operation == 'reserve' else True
    engine = MagicMock(spec=Engine)
    engine.begin.return_value.__enter__.return_value = connection
    user = SimpleNamespace(user_id=uuid4())

    def authenticate(*args: object, **kwargs: object) -> SimpleNamespace:
        assert connection.exec_driver_sql.call_args_list == [
            call("SET LOCAL statement_timeout='10s'"), call("SET LOCAL lock_timeout='5s'")]
        return user

    monkeypatch.setattr(llm_budget, 'AuthSessionRepository', lambda c: SimpleNamespace(authenticate=authenticate))
    monkeypatch.setattr(llm_budget, 'bind_authenticated_tenant', lambda *a, **k: SimpleNamespace(identity=user))
    if operation == 'reserve':
        llm_budget.reserve_inference(cast(Engine, engine), session_cookie='s' * 43, csrf='csrf',
            organization_id=uuid4(), model='synthetic', input_chars=10)
    elif operation == 'finish':
        llm_budget.finish_inference(cast(Engine, engine), request_id=uuid4(), session_cookie='s' * 43, succeeded=False)
    else:
        llm_budget.seal_inference_preview(cast(Engine, engine), request_id=uuid4(),
            session_cookie='s' * 43, source='synthetic', extracted={'synthetic': True})
    assert connection.exec_driver_sql.call_args_list == [
        call("SET LOCAL statement_timeout='10s'"), call("SET LOCAL lock_timeout='5s'")]


@pytest.mark.parametrize('operation', ['claim', 'publish', 'release'])
def test_cache_sql_has_timeouts_before_authentication(
    monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    connection = MagicMock(spec=Connection)
    connection.execute.return_value.one.return_value = ('miss', None)
    engine = MagicMock(spec=Engine)
    engine.begin.return_value.__enter__.return_value = connection

    def authenticated(*args: object, **kwargs: object) -> None:
        assert connection.exec_driver_sql.call_args_list == [
            call("SET LOCAL statement_timeout='10s'"), call("SET LOCAL lock_timeout='5s'")]

    monkeypatch.setattr(llm_cache, 'bind_authenticated_tenant', authenticated)
    monkeypatch.setattr(llm_cache, 'encrypt_cached_payload', lambda _: b'synthetic')
    kwargs = {'organization_id': uuid4(), 'attempt_id': uuid4(), 'session_cookie': 's' * 43,
              'csrf': 'csrf', 'request_hmac': 'a' * 64}
    if operation == 'claim':
        llm_cache.claim_cached_extraction(cast(Engine, engine), **kwargs, model='synthetic')  # type: ignore[arg-type]
    elif operation == 'publish':
        llm_cache.publish_cached_extraction(cast(Engine, engine), **kwargs, extracted={})  # type: ignore[arg-type]
    else:
        llm_cache.release_cached_extraction(cast(Engine, engine), **kwargs)  # type: ignore[arg-type]
