"""Identify strictly marked temporary clones of the live 0015 PostgreSQL schema."""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import Connection, text

_CLONE = re.compile(r"kpdesk_accounts_http_[0-9a-f]{12}\Z")


def is_live_accounts_http_clone(connection: Connection) -> bool:
    name = connection.scalar(text("SELECT current_database()"))
    if not isinstance(name, str) or _CLONE.fullmatch(name) is None:
        return False
    marker = connection.scalar(text("""
        SELECT shobj_description(oid,'pg_database')
        FROM pg_database WHERE datname=:name
    """), {"name": name})
    return bool(marker == "kpdesk:accounts-http:" + name.rsplit("_", 1)[-1])



@dataclass(frozen=True, slots=True)
class AuthDatabaseEnvironment:
    """Transaction-local database/role metadata; never cached across connections."""

    database: str
    role: str
    is_marked_clone: bool
    gateway_available: bool
    tenant_rls_available: bool
    split_test_role_available: bool


_AUTH_ENVIRONMENT = text("""
    SELECT current_database(), current_user,
           to_regprocedure('public.kpdesk_auth_credential(text,text)') IS NOT NULL,
           to_regprocedure('public.kpdesk_session_tenant_allowed(uuid,text)') IS NOT NULL,
           EXISTS(SELECT 1 FROM pg_catalog.pg_roles
                  WHERE rolname='kpdesk_auth_test'),
           (SELECT shobj_description(oid, 'pg_database')
              FROM pg_catalog.pg_database
             WHERE datname=current_database())
""")


def authenticated_database_environment(connection: Connection) -> AuthDatabaseEnvironment:
    """Resolve all metadata guards with one PostgreSQL round trip.

    This is a read-only, always-refreshed security check, NOT authenticated
    tenant access. Callers must still validate the live session and evaluate
    kpdesk_session_tenant_allowed in the same transaction before data reads.
    """
    database, role, gateway, rls, split, marker = connection.execute(
        _AUTH_ENVIRONMENT,
    ).one()
    if not isinstance(database, str) or not isinstance(role, str):
        raise TypeError("PostgreSQL role/database identity unavailable.")
    marked = bool(
        _CLONE.fullmatch(database)
        and marker == "kpdesk:accounts-http:" + database.rsplit("_", 1)[-1]
    )
    return AuthDatabaseEnvironment(
        database=database, role=role, is_marked_clone=marked,
        gateway_available=gateway is True,
        tenant_rls_available=rls is True,
        split_test_role_available=split is True,
    )
