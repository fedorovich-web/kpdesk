"""Explicitly committed login transaction, independent from HTTP failure handling.

Keep credential-check counter updates even when password verification fails.
The HTTP response (including the Secure session cookie) is issued only AFTER
database transaction commit has succeeded. The Engine must connect using the
dedicated auth identity, not the catalog runtime DB role.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine

from kpdesk.db.auth_sessions import AuthSessionRepository, IssuedSession, LoginDenied


@dataclass(frozen=True, slots=True)
class CommittedLogin:
    auth_engine: Engine

    def attempt(self, email: str, password: str, *, peer: str) -> IssuedSession:
        issued: IssuedSession | None = None
        # Deliberately suppress ONLY invalid credentials while inside the DB
        # context. SQL/server errors must propagate and roll back. Never send
        # a successful login cookie until the context manager commits.
        with self.auth_engine.begin() as connection:
            try:
                issued = AuthSessionRepository(connection).login(
                    email, password, peer=peer,
                )
            except LoginDenied:
                pass
        if issued is None:
            raise LoginDenied("Invalid login.")
        return issued



@dataclass(frozen=True, slots=True)
class CommittedSessionRevocation:
    """Privileged logout is committed by the separate auth database Engine."""

    auth_engine: Engine

    def revoke(self, raw_token: str) -> bool:
        with self.auth_engine.begin() as connection:
            revoked = AuthSessionRepository(connection).revoke(raw_token)
        return revoked
