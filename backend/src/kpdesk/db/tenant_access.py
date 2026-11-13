"""Tenant authorization remains internal until authenticated HTTP sessions exist.

Accept only a trusted server-side authenticated user identity; never derive it
from a request-supplied user/org header. Membership and transaction-local
context must be revalidated on every operation. No schema or DB writes here.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import Connection, text

type TenantOperation = Literal["read", "write", "manage"]
type TenantRole = Literal["owner", "editor", "viewer"]


class TenantAccessDenied(Exception):
    """No valid active organization membership for this action."""


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    """Must originate from a server-verified session, NOT user-supplied JSON."""

    user_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class AuthorizedContext:
    user_id: uuid.UUID
    organization_id: uuid.UUID
    role: TenantRole


_ALLOWED_ROLES: dict[TenantOperation, frozenset[TenantRole]] = {
    "read": frozenset(("owner", "editor", "viewer")),
    "write": frozenset(("owner", "editor")),
    "manage": frozenset(("owner",)),
}

_SQL = text("""
    SELECT membership.role
    FROM organization_memberships AS membership
    JOIN organizations AS organization ON organization.id = membership.organization_id
    JOIN users AS person ON person.id = membership.user_id
    WHERE membership.organization_id = :org
      AND membership.user_id = :person
      AND membership.status = 'active'
      AND organization.status = 'active'
      AND person.status = 'active'
      AND person.email_verified_at IS NOT NULL
""")


def authorize_tenant_transaction(
    connection: Connection,
    identity: AuthenticatedUser,
    organization_id: uuid.UUID,
    operation: TenantOperation,
) -> AuthorizedContext:
    """Validate membership then bind the selected org to this *transaction only*.

    Caller must establish a transaction and authenticate identity separately.
    Tenant IDs supplied by clients are never proof of authorization. A rejected
    attempt clears any previous LOCAL tenant context in this transaction.
    """
    if not connection.in_transaction():
        raise RuntimeError("Tenant context requires an explicit active transaction.")
    connection.execute(
        text("SELECT set_config('kpdesk.organization_id', '', true)")
    )
    if (
        not isinstance(identity, AuthenticatedUser)
        or not isinstance(identity.user_id, uuid.UUID)
        or not isinstance(organization_id, uuid.UUID)
        or operation not in _ALLOWED_ROLES
    ):
        raise TenantAccessDenied("Tenant membership denied.")
    # A5+ credential gateway removes broad app SELECT on users/memberships.
    # Earlier A3 test-only transactions still exercise the legacy SQL path.
    scoped_auth_gateway = connection.scalar(text("""
        SELECT to_regprocedure('public.kpdesk_auth_member(uuid,uuid)') IS NOT NULL
    """))
    if scoped_auth_gateway:
        role_value: object = connection.scalar(text("""
            SELECT public.kpdesk_auth_member(:person, :org)
        """), {"org": organization_id, "person": identity.user_id})
    else:
        role_value = connection.execute(
            _SQL, {"org": organization_id, "person": identity.user_id},
        ).scalar_one_or_none()
    allowed = _ALLOWED_ROLES[operation]
    if role_value not in allowed:
        raise TenantAccessDenied("Tenant membership denied.")
    role = role_value  # Narrowed to the closed literal role allowlist.
    # Apply a transaction-local GUC; pooled connection defaults never change.
    connection.execute(
        text("SELECT set_config('kpdesk.organization_id', :org, true)"),
        {"org": str(organization_id)},
    )
    return AuthorizedContext(identity.user_id, organization_id, role)



def authorize_test_tenant_runtime(
    connection: Connection,
    identity: AuthenticatedUser,
    organization_id: uuid.UUID,
    operation: TenantOperation,
) -> AuthorizedContext:
    """Strict A3-only test boundary, NOT a public SaaS session implementation.

    The real production path will require an approved migration, least-privilege
    grants and sessions. This never falls back to database owner permissions.
    """
    if not connection.in_transaction():
        raise RuntimeError("A3 tenant runtime requires a caller-owned transaction.")
    database, role, forced = connection.execute(text("""
        SELECT current_database(), current_user,
          (SELECT relforcerowsecurity
           FROM pg_class WHERE oid='catalog_versions'::regclass)
    """)).one()
    if database != "kpdesk_tenant_test" or role != "kpdesk_app" or forced is not True:
        raise RuntimeError("A4 repository is restricted to A3 test/runtime role.")
    return authorize_tenant_transaction(
        connection, identity, organization_id, operation,
    )
