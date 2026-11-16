"""Narrow Yandex gateways. The caller owns and commits each transaction.

Domain errors returned by SQL can represent a consumed flow. Catch
YandexStorageError *inside* the transaction context, commit, and only then surface
the error. Unexpected/SQLAlchemy exceptions must instead roll back. In particular
claim must commit before any provider HTTP await, and complete before cookies.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import Connection, text

_HASH = re.compile(r'[0-9a-f]{64}\Z')
_EMAIL = re.compile(r'[a-z0-9][a-z0-9._%+\-]{0,63}@[a-z0-9.-]+\.[a-z]{2,30}\Z')
_ERRORS = frozenset({'invalid_request', 'flow_invalid', 'link_required', 'account_disabled',
                     'session_invalid', 'identity_conflict', 'rate_limited', 'email_required',
                     'storage_unavailable'})


class YandexStorageError(ValueError):
    """Allowlisted domain error, safe to expose without SQL/provider detail."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _ERRORS else 'storage_unavailable'
        super().__init__(self.code)


def _bounded(value: object, minimum: int, maximum: int) -> bool:
    return isinstance(value, str) and minimum <= len(value) <= maximum and '\x00' not in value


def _hash(value: object) -> bool:
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


class YandexStore:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def _call(self, function: str, parameters: dict[str, object]) -> dict[str, object]:
        # Names are internal constants, never caller-controlled SQL identifiers.
        arguments = ','.join(':' + key for key in parameters)
        result = self.connection.scalar(text(f'SELECT public.{function}({arguments})'), parameters)
        if not isinstance(result, dict):
            raise YandexStorageError('storage_unavailable')
        if 'error' in result:
            code = result['error']
            raise YandexStorageError(code if isinstance(code, str) else 'storage_unavailable')
        return cast(dict[str, object], result)

    def issue(self, *, state_hash: str, browser_hash: str, verifier_cipher: str,
              mode: str, next_path: str, peer: str, session_hash: str | None = None,
              csrf_hash: str | None = None) -> None:
        if (not _hash(state_hash) or not _hash(browser_hash)
                or not _bounded(verifier_cipher, 80, 4096) or not _bounded(peer, 1, 64)
                or not isinstance(mode, str) or mode not in ('login', 'link')
                or not isinstance(next_path, str) or next_path not in ('/app', '/admin')
                or (mode == 'link' and (not _hash(session_hash) or not _hash(csrf_hash)))
                or (mode == 'login' and (session_hash is not None or csrf_hash is not None))):
            raise YandexStorageError('invalid_request')
        self._call('kpdesk_yandex_issue', {'state_hash': state_hash, 'browser_hash': browser_hash,
                   'verifier_cipher': verifier_cipher, 'mode': mode, 'next_path': next_path, 'peer': peer,
                   'session_hash': session_hash, 'csrf_hash': csrf_hash})

    def claim(self, *, state_hash: str, browser_hash: str, claim_hash: str) -> dict[str, object]:
        if not all(_hash(value) for value in (state_hash, browser_hash, claim_hash)):
            raise YandexStorageError('invalid_request')
        return self._call('kpdesk_yandex_claim', {'state_hash': state_hash,
                          'browser_hash': browser_hash, 'claim_hash': claim_hash})

    def complete(self, *, state_hash: str, claim_hash: str, client_id: str, subject: str,
                 email: str | None, session_hash: str, csrf_hash: str,
                 expires_at: datetime) -> dict[str, object]:
        if (not all(_hash(value) for value in (state_hash, claim_hash, session_hash, csrf_hash))
                or not _bounded(client_id, 1, 255) or not _bounded(subject, 1, 255)
                or (email is not None and (not _bounded(email, 3, 254)
                    or not isinstance(email, str) or _EMAIL.fullmatch(email) is None))
                or not isinstance(expires_at, datetime) or expires_at.utcoffset() is None
                or not datetime.now(UTC) < expires_at <= datetime.now(UTC) + timedelta(days=7)):
            raise YandexStorageError('invalid_request')
        return self._call('kpdesk_yandex_complete', {'state_hash': state_hash,
                          'claim_hash': claim_hash, 'client_id': client_id, 'subject': subject, 'email': email,
                          'session_hash': session_hash, 'csrf_hash': csrf_hash, 'expires_at': expires_at})

    def identity(self, *, session_hash: str, client_id: str) -> bool:
        if not _hash(session_hash) or not _bounded(client_id, 1, 255):
            raise YandexStorageError('invalid_request')
        return self.connection.scalar(text(
            'SELECT public.kpdesk_yandex_identity(:session_hash,:client_id)'),
            {'session_hash': session_hash, 'client_id': client_id}) is True
