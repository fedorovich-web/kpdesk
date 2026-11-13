"""Argon2id login and SHA256-indexed PostgreSQL sessions.

Separate live PostgreSQL auth/tenant roles and the original A8 test roles
are explicitly guarded. No public login is exposed by the default app; the
optional HTTPS session app requires verified users, CSRF and Origin checks.
The server, never the browser, owns database connection identity.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import secrets
import uuid
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import Connection, text

from kpdesk.db.live_rehearsal_scope import authenticated_database_environment
from kpdesk.db.tenant_access import AuthenticatedUser
from kpdesk.db.test_database_scope import is_auth_test_database

# 64 MiB + time-cost 3; Argon2id avoids fast offline password guessing.
_PASSWORD_HASHER = PasswordHasher(
    time_cost=3, memory_cost=65536, parallelism=2,
    hash_len=32, salt_len=16, type=Type.ID,
)
# A precomputed dummy hash is used for missing/disabled/unverified accounts
# so error behavior does not reveal account existence. Generated once locally.
_FAKE_HASH = _PASSWORD_HASHER.hash("nonexistent-account-dummy-password")
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_EMAIL_PATTERN = re.compile(r"[a-z0-9][a-z0-9._%+\-]{0,63}@[a-z0-9.-]{1,180}\.[a-z]{2,30}\Z")
SESSION_LIFETIME = timedelta(days=7)  # Absolute expiry; logout/reset still revoke.


class LoginDenied(Exception):
    """Generic failure; caller must never disclose whether email exists."""


class SessionDenied(Exception):
    """Session is expired, revoked, invalid, or lacking required CSRF token."""


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """Raw secrets are returned exactly once; never log or persist plaintext."""

    session_token: str
    csrf_token: str
    user_id: uuid.UUID
    expires_at: datetime


def _token_digest(token: str) -> str:
    if not isinstance(token, str) or not _TOKEN_PATTERN.fullmatch(token):
        raise SessionDenied("Invalid or expired session.")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def csrf_for_session(raw_token: str) -> str:
    """Recreate the CSRF token without exposing/replacing the HttpOnly cookie.

    The 256-bit random session token is the HMAC key. A different domain
    separator prevents reuse as an authentication token. Only its SHA-256 is
    stored in PostgreSQL, permitting safe recovery after browser reload.
    """
    _token_digest(raw_token)
    signature = hmac.new(
        raw_token.encode("ascii"), b"kpdesk-csrf-v1", hashlib.sha256,
    ).digest()
    return urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")


def hash_password(password: str) -> str:
    """Only for verified account provisioning and password-reset workflows."""
    if (
        not isinstance(password, str)
        or not 12 <= len(password) <= 256
        or not 12 <= len(password.encode("utf-8")) <= 1024
        or "\x00" in password
    ):
        raise ValueError("Password does not satisfy server password policy.")
    return _PASSWORD_HASHER.hash(password)


def verify_password(stored_hash: str | None, attempted: str) -> bool:
    if not isinstance(attempted, str) or len(attempted) > 256:
        return False
    candidate = stored_hash if isinstance(stored_hash, str) else _FAKE_HASH
    try:
        return bool(_PASSWORD_HASHER.verify(candidate, attempted)) and stored_hash is not None
    except (VerificationError, InvalidHashError, ValueError, TypeError):
        return False


def _normalized_email(value: str) -> str:
    if not isinstance(value, str):
        raise LoginDenied("Invalid login.")
    email = value.strip().lower()
    if len(email) > 254 or not _EMAIL_PATTERN.fullmatch(email):
        raise LoginDenied("Invalid login.")
    return email


def _test_schema_guard(
    connection: Connection, operation: Literal["issue", "validate", "revoke"],
) -> None:
    if not connection.in_transaction():
        raise RuntimeError("Session repository requires an explicit transaction.")
    environment = authenticated_database_environment(connection)
    if not environment.gateway_available:
        raise RuntimeError("Authentication gateway is not installed.")
    if environment.database == "kpdesk" or environment.is_marked_clone:
        if not environment.tenant_rls_available:
            raise RuntimeError("Live authenticated RLS function is unavailable.")
        allowed_roles = (
            ("kpdesk_auth_runtime", "kpdesk_tenant_runtime")
            if operation == "validate" else ("kpdesk_auth_runtime",)
        )
    elif is_auth_test_database(connection):
        if environment.split_test_role_available:
            allowed_roles = (
                ("kpdesk_auth_test", "kpdesk_app")
                if operation == "validate" else ("kpdesk_auth_test",)
            )
        else:
            allowed_roles = ("kpdesk_app",)
    else:
        raise RuntimeError("Unrecognized authentication database.")
    if environment.role not in allowed_roles:
        raise RuntimeError("Database role is not allowed to perform this auth operation.")


@dataclass(frozen=True, slots=True)
class AuthSessionRepository:
    connection: Connection

    def login(self, email: str, password: str, *, peer: str) -> IssuedSession:
        _test_schema_guard(self.connection, "issue")
        normalized = _normalized_email(email)
        try:
            normalized_peer = str(ipaddress.ip_address(peer))
        except (TypeError, ValueError) as error:
            raise LoginDenied("Invalid login.") from error
        # A SECURITY DEFINER gateway enforces the account/peer rate limits in
        # PostgreSQL. App role has NO SELECT on users or login attempt counters.
        user = self.connection.execute(text("""
            SELECT identity_id, secret_hash, usable, limited
            FROM public.kpdesk_auth_credential(:email, :peer)
        """), {"email": normalized, "peer": normalized_peer}).one_or_none()
        if user is not None and user.limited:
            raise LoginDenied("Invalid login.")
        correct = verify_password(user.secret_hash if user is not None else None, password)
        if user is None or not user.usable or not correct:
            raise LoginDenied("Invalid login.")
        token = secrets.token_urlsafe(32)
        csrf = csrf_for_session(token)
        expires = datetime.now(UTC) + SESSION_LIFETIME
        self.connection.execute(text("""
            INSERT INTO auth_sessions(token_sha256, user_id, csrf_sha256, expires_at)
            VALUES (:token_hash, :user_id, :csrf_hash, :expires)
        """), {
            "token_hash": _token_digest(token), "user_id": user.identity_id,
            "csrf_hash": _token_digest(csrf), "expires": expires,
        })
        return IssuedSession(token, csrf, user.identity_id, expires)

    def authenticate(
        self, raw_token: str, *,
        operation: Literal["read", "write"] = "read",
        csrf: str | None = None,
    ) -> AuthenticatedUser:
        _test_schema_guard(self.connection, "validate")
        if operation not in ("read", "write"):
            raise SessionDenied("Invalid session operation.")
        token_hash = _token_digest(raw_token)
        row = self.connection.execute(text("""
            SELECT identity_id, csrf_hash
            FROM public.kpdesk_auth_session(:hash)
        """), {"hash": token_hash}).one_or_none()
        if row is None:
            raise SessionDenied("Invalid or expired session.")
        if operation == "write":
            csrf_hash = _token_digest(csrf) if csrf is not None else None
            if csrf_hash is None or not secrets.compare_digest(
                csrf_hash, row.csrf_hash,
            ):
                raise SessionDenied("Invalid or missing CSRF token.")
        return AuthenticatedUser(row.identity_id)

    def revoke(self, raw_token: str) -> bool:
        _test_schema_guard(self.connection, "revoke")
        token_hash = _token_digest(raw_token)
        # Only the owner SECURITY DEFINER function can update session rows.
        result = self.connection.scalar(text("""
            SELECT public.kpdesk_auth_revoke(:hash)
        """), {"hash": token_hash})
        return result is True
