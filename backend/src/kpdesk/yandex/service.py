"""Commit-before-HTTP/response orchestration for optional Yandex authentication."""
import ipaddress
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import Engine
from starlette.concurrency import run_in_threadpool

from kpdesk.db.auth_sessions import (
    SESSION_LIFETIME,
    AuthSessionRepository,
    IssuedSession,
    csrf_for_session,
)
from kpdesk.yandex.client import YandexClient, YandexError
from kpdesk.yandex.schema import yandex_available
from kpdesk.yandex.settings import YandexSettings
from kpdesk.yandex.state import VerifierCipher, digest_token, pkce_challenge, safe_next_path
from kpdesk.yandex.storage import YandexStorageError, YandexStore

_DOMAIN_ERRORS = {
    'invalid_request': 'yandex_flow_expired', 'flow_invalid': 'yandex_flow_expired',
    'link_required': 'yandex_account_link_required', 'account_disabled': 'yandex_account_unavailable',
    'session_invalid': 'yandex_flow_expired', 'identity_conflict': 'yandex_identity_conflict',
    'rate_limited': 'yandex_rate_limited', 'email_required': 'yandex_email_required',
    'storage_unavailable': 'yandex_unavailable',
}


@dataclass(frozen=True, slots=True)
class OAuthStart:
    authorization_url: str = field(repr=False)
    browser_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ClaimedAttempt:
    state_hash: str = field(repr=False)
    claim_hash: str = field(repr=False)
    verifier: str = field(repr=False)
    mode: Literal['login', 'link']
    next_path: str


@dataclass(frozen=True, slots=True)
class OAuthOutcome:
    session: IssuedSession = field(repr=False)
    next_path: str


class YandexService:
    def __init__(self, settings: YandexSettings, auth: Engine | None, tenant: Engine | None,
                 provider: YandexClient | None = None,
                 cipher_factory: Callable[[], VerifierCipher] | None = None) -> None:
        self.settings, self.auth, self.tenant = settings, auth, tenant
        self.provider = provider if provider is not None else YandexClient(settings)
        self._cipher = cipher_factory if cipher_factory is not None else VerifierCipher.from_runtime

    def provider_available(self) -> bool:
        if not self.settings.enabled:
            return False
        self._cipher()
        if self.auth is None:
            raise YandexError('yandex_unavailable')
        with self.auth.begin() as connection:
            connection.exec_driver_sql('SET TRANSACTION READ ONLY')
            if not yandex_available(connection):
                raise YandexError('yandex_unavailable')
        return True

    def _ready(self) -> None:
        if not self.provider_available():
            raise YandexError('yandex_disabled')

    def _committed[T](self, operation: Callable[[YandexStore], T]) -> T:
        if self.auth is None:
            raise YandexError('yandex_unavailable')
        failure: YandexStorageError | None = None
        value: T | None = None
        # Domain errors may represent a consumed flow; preserve that transaction.
        # Unexpected SQL/commit errors propagate, so a response cannot issue cookies.
        with self.auth.begin() as connection:
            try:
                value = operation(YandexStore(connection))
            except YandexStorageError as error:
                failure = error
        if failure is not None:
            raise YandexError(_DOMAIN_ERRORS[failure.code]) from None
        return cast(T, value)

    def start(self, mode: Literal['login', 'link'], next_path: str | None, peer: str,
              session: str | None = None, csrf: str | None = None) -> OAuthStart:
        self._ready()
        try:
            normalized_peer = str(ipaddress.ip_address(peer))
        except ValueError:
            raise YandexError('yandex_request_invalid') from None
        session_hash = digest_token(session or '') if mode == 'link' else None
        csrf_hash = digest_token(csrf or '') if mode == 'link' else None
        state, browser, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        url = self.provider.authorization_url(state, pkce_challenge(verifier))
        cipher = self._cipher().seal(verifier)
        self._committed(lambda store: store.issue(state_hash=digest_token(state),
            browser_hash=digest_token(browser), verifier_cipher=cipher, mode=mode,
            next_path=safe_next_path(next_path), peer=normalized_peer,
            session_hash=session_hash, csrf_hash=csrf_hash))
        return OAuthStart(url, browser)

    def claim(self, state: str, browser: str) -> ClaimedAttempt:
        state_hash, browser_hash = digest_token(state), digest_token(browser)
        self._ready()
        claim_hash = digest_token(secrets.token_urlsafe(32))
        value = self._committed(lambda store: store.claim(state_hash=state_hash,
            browser_hash=browser_hash, claim_hash=claim_hash))
        mode, target, sealed = value.get('mode'), value.get('next_path'), value.get('verifier_cipher')
        if mode not in ('login', 'link') or target not in ('/app', '/admin') or not isinstance(sealed, str):
            raise YandexError('yandex_unavailable')
        return ClaimedAttempt(state_hash, claim_hash, self._cipher().open(sealed),
                              mode, target)

    async def finish(self, attempt: ClaimedAttempt, code: str | None, error: str | None) -> OAuthOutcome:
        if error:
            raise YandexError('yandex_cancelled')
        if not code:
            raise YandexError('yandex_flow_expired')
        profile = await self.provider.exchange_and_profile(code, attempt.verifier)
        token = secrets.token_urlsafe(32)
        csrf = csrf_for_session(token)
        expires = datetime.now(UTC) + SESSION_LIFETIME
        value = await run_in_threadpool(self._committed, lambda store: store.complete(
            state_hash=attempt.state_hash, claim_hash=attempt.claim_hash,
            client_id=profile.client_id, subject=profile.subject, email=profile.email,
            session_hash=digest_token(token), csrf_hash=digest_token(csrf), expires_at=expires))
        try:
            user = UUID(str(value['user_id']))
            if not isinstance(value['created'], bool):
                raise TypeError('invalid_creation_flag')
        except (ValueError, TypeError, KeyError):
            raise YandexError('yandex_unavailable') from None
        return OAuthOutcome(IssuedSession(token, csrf, user, expires), attempt.next_path)

    def identity(self, session: str) -> bool:
        self._ready()
        if self.tenant is None:
            raise YandexError('yandex_unavailable')
        with self.tenant.begin() as connection:
            connection.exec_driver_sql('SET TRANSACTION READ ONLY')
            AuthSessionRepository(connection).authenticate(session)
            return YandexStore(connection).identity(session_hash=digest_token(session),
                                                     client_id=self.settings.client_id)
