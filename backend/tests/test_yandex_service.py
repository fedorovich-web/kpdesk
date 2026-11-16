"""OAuth orchestration commits claims before HTTP and sessions before cookies."""
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import Engine as SqlAlchemyEngine

from kpdesk.yandex.client import YandexClient, YandexError, YandexProfile
from kpdesk.yandex.service import YandexService
from kpdesk.yandex.settings import YandexSettings
from kpdesk.yandex.state import VerifierCipher, digest_token
from kpdesk.yandex.storage import YandexStorageError

CLIENT = 'a' * 32


class Transaction:
    def __init__(self, engine: Any) -> None:
        self.engine = engine

    def __enter__(self) -> Any:
        assert not self.engine.active
        self.engine.active = True
        self.engine.events.append('begin')
        return Mock()

    def __exit__(self, kind: type[BaseException] | None, value: BaseException | None,
                 trace: TracebackType | None) -> None:
        self.engine.active = False
        self.engine.events.append('rollback' if kind else 'commit')
        if not kind and self.engine.commit_error:
            raise RuntimeError('private-commit-detail')


class Engine:
    def __init__(self) -> None:
        self.active = False
        self.events: list[str] = []
        self.commit_error = False

    def begin(self) -> Any:
        return Transaction(self)


def context(monkeypatch: pytest.MonkeyPatch) -> Any:
    engine = Engine()
    cipher = VerifierCipher(Fernet.generate_key())
    store = Mock()
    store.claim.return_value = {'verifier_cipher': cipher.seal('v' * 64), 'mode': 'login',
                               'next_path': '/app', 'expires_at': (datetime.now(UTC) + timedelta(minutes=5)).isoformat()}
    store.complete.return_value = {'user_id': str(uuid4()), 'created': True}
    monkeypatch.setattr('kpdesk.yandex.service.YandexStore', lambda _: store)
    monkeypatch.setattr('kpdesk.yandex.service.yandex_available', lambda _: True)

    class Provider:
        def authorization_url(self, state: Any, challenge: Any) -> Any:
            return 'https://oauth.yandex.ru/authorize?state=' + state

        async def exchange_and_profile(self, code: Any, verifier: Any) -> Any:
            assert not engine.active, 'Provider await retained a database transaction'
            assert engine.events[-1] == 'commit', 'Claim was not durably committed'
            assert (code, verifier) == ('synthetic-code', 'v' * 64)
            engine.events.append('provider')
            return YandexProfile('12345', CLIENT, 'buyer@example.test', None)

    settings = YandexSettings(enabled=True, client_id=CLIENT, client_secret='synthetic-secret-value')
    service = YandexService(settings, cast(SqlAlchemyEngine, engine),
                            cast(SqlAlchemyEngine, engine), cast(YandexClient, Provider()),
                            lambda: cipher)
    return service, engine, store


def test_disabled_provider_is_available_without_database_or_secretfile() -> None:
    service = YandexService(YandexSettings(), None, None)
    assert service.provider_available() is False
    with pytest.raises(YandexError, match='yandex_disabled'):
        service.start('login', '/app', '198.51.100.1')


def test_start_commits_only_hashes_and_encrypted_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    service, engine, store = context(monkeypatch)
    result = service.start('login', '/admin', '198.51.100.1')
    parameters = store.issue.call_args.kwargs
    assert parameters['next_path'] == '/admin' and parameters['mode'] == 'login'
    assert len(parameters['state_hash']) == len(parameters['browser_hash']) == 64
    assert result.browser_token not in repr(result)
    assert result.browser_token not in str(parameters)
    assert engine.events[-1] == 'commit'


@pytest.mark.asyncio
async def test_claim_http_complete_have_separate_committed_transactions(monkeypatch: pytest.MonkeyPatch) -> None:
    service, engine, store = context(monkeypatch)
    attempt = service.claim('s' * 43, 'b' * 43)
    result = await service.finish(attempt, 'synthetic-code', None)
    assert result.next_path == '/app'
    assert len(result.session.session_token) == len(result.session.csrf_token) == 43
    parameters = store.complete.call_args.kwargs
    assert parameters['session_hash'] == digest_token(result.session.session_token)
    assert parameters['csrf_hash'] == digest_token(result.session.csrf_token)
    assert result.session.session_token not in str(parameters)
    assert engine.events[-1] == 'commit'
    assert engine.events.index('provider') > engine.events.index('commit')


@pytest.mark.asyncio
async def test_domain_failure_consumption_commits_before_error(monkeypatch: pytest.MonkeyPatch) -> None:
    service, engine, store = context(monkeypatch)
    attempt = service.claim('s' * 43, 'b' * 43)
    store.complete.side_effect = YandexStorageError('link_required')
    with pytest.raises(YandexError, match='yandex_account_link_required'):
        await service.finish(attempt, 'synthetic-code', None)
    assert engine.events[-1] == 'commit'


@pytest.mark.asyncio
async def test_commit_failure_never_returns_issued_session(monkeypatch: pytest.MonkeyPatch) -> None:
    service, engine, _ = context(monkeypatch)
    attempt = service.claim('s' * 43, 'b' * 43)
    engine.commit_error = True
    with pytest.raises(RuntimeError):
        await service.finish(attempt, 'synthetic-code', None)


@pytest.mark.asyncio
async def test_cancelled_provider_flow_does_not_exchange(monkeypatch: pytest.MonkeyPatch) -> None:
    service, engine, store = context(monkeypatch)
    attempt = service.claim('s' * 43, 'b' * 43)
    with pytest.raises(YandexError, match='yandex_cancelled'):
        await service.finish(attempt, None, 'access_denied')
    assert 'provider' not in engine.events and not store.complete.called


def test_wrong_browser_claim_fails_after_committed_denial(monkeypatch: pytest.MonkeyPatch) -> None:
    service, engine, store = context(monkeypatch)
    store.claim.side_effect = YandexStorageError('flow_invalid')
    with pytest.raises(YandexError, match='yandex_flow_expired'):
        service.claim('s' * 43, 'b' * 43)
    assert engine.events[-1] == 'commit'


def test_missing_state_stops_before_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    service, _, store = context(monkeypatch)
    with pytest.raises(YandexError):
        service.claim('', '')
    store.claim.assert_not_called()
