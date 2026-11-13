"""Authenticated session-bound PostgreSQL tenant selection for live/A8.

Never trust organization headers or mutable GUCs as authentication.
PostgreSQL policies verify the active session digest, membership and role;
a transaction-local context alone cannot authorize access. Caller owns
the transaction and supplies the cryptographically random session cookie.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import Connection, text

from kpdesk.db.auth_sessions import AuthSessionRepository, SessionDenied
from kpdesk.db.live_rehearsal_scope import authenticated_database_environment
from kpdesk.db.tenant_access import AuthenticatedUser, TenantAccessDenied, TenantOperation
from kpdesk.db.test_database_scope import is_auth_test_database

_BEARER = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_CLEAR = text("""
    SELECT set_config('kpdesk.session_sha256', '', true),
           set_config('kpdesk.organization_id', '', true)
""")


@dataclass(frozen=True, slots=True)
class AuthenticatedTenantScope:
    identity: AuthenticatedUser
    organization_id: uuid.UUID
    operation: TenantOperation


def bind_authenticated_tenant(
    connection: Connection,
    *,
    session_token: str,
    organization_id: uuid.UUID,
    operation: TenantOperation,
    csrf: str | None = None,
) -> AuthenticatedTenantScope:
    """Bind a session for RLS in one open transaction, rechecking membership.

    A failed selection clears prior session/org settings to prevent connection
    reuse from leaking the previous tenant context. The source session must be
    a cryptographically random opaque cookie, never the caller's user UUID.
    """
    if not connection.in_transaction():
        raise RuntimeError("Authenticated RLS requires a caller-owned transaction.")
    connection.execute(_CLEAR)
    environment = authenticated_database_environment(connection)
    if environment.database == "kpdesk" or environment.is_marked_clone:
        allowed = environment.role == "kpdesk_tenant_runtime"
    else:
        allowed = (
            is_auth_test_database(connection)
            and environment.role == "kpdesk_app"
        )
    if not allowed or not environment.tenant_rls_available:
        raise RuntimeError("Authenticated tenant RLS is not available for this role.")
    if (
        not isinstance(organization_id, uuid.UUID)
        or operation not in ("read", "write", "manage")
        or not isinstance(session_token, str)
        or _BEARER.fullmatch(session_token) is None
    ):
        raise TenantAccessDenied("Invalid authenticated tenant request.")
    check: Literal["read", "write"] = "read" if operation == "read" else "write"
    # The session verifier checks expiry, disabled accounts, and CSRF on writes.
    try:
        person = AuthSessionRepository(connection).authenticate(
            session_token, operation=check, csrf=csrf,
        )
    except SessionDenied as error:
        raise TenantAccessDenied("Invalid authenticated session.") from error
    digest = hashlib.sha256(session_token.encode("ascii")).hexdigest()
    connection.execute(text("""
        SELECT set_config('kpdesk.session_sha256', :digest, true)
    """), {"digest": digest})
    allowed = connection.scalar(text("""
        SELECT public.kpdesk_session_tenant_allowed(:org, :action)
    """), {"org": organization_id, "action": operation})
    if allowed is not True:
        connection.execute(_CLEAR)
        raise TenantAccessDenied("Organization membership denied.")
    return AuthenticatedTenantScope(person, organization_id, operation)
