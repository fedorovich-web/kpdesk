"""A3/A4 prerequisite: real PostgreSQL membership and SET LOCAL semantics.

Each test owns a marked disposable clone of the additive tenant schema.
No production database, auth token or network provider.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from postgres_clone_connection import (
    LEGACY_TEST_OPT_IN,
    legacy_test_database,
    require_legacy_test_clone,
)
from sqlalchemy import Connection, text

from kpdesk.db.tenant_access import (
    AuthenticatedUser,
    TenantAccessDenied,
    authorize_tenant_transaction,
)

pytestmark = pytest.mark.skipif(
    os.environ.get(LEGACY_TEST_OPT_IN) != "1",
    reason="Explicit opt-in required for per-test marked disposable PostgreSQL clones",
)


@pytest.fixture
def tenant_connection() -> Iterator[tuple[Connection, dict[str, uuid.UUID]]]:
    ids = {name: uuid.uuid4() for name in (
        "a", "b", "suspended", "legacy_locked", "owner", "editor", "viewer", "pending", "revoked", "disabled",
    )}
    with legacy_test_database() as engine, engine.connect() as connection:
        transaction = connection.begin()
        try:
            require_legacy_test_clone(connection, str(engine.url.database))
            if connection.scalar(text("SELECT version_num FROM alembic_version")) != "0005_tenant_expand":
                raise RuntimeError("Membership fixture requires the additive tenant schema.")
            for name, status in (
                ("a", "active"), ("b", "active"), ("suspended", "suspended"), ("legacy_locked", "legacy_locked"),
            ):
                connection.execute(text("""
                    INSERT INTO organizations (id, name, status)
                    VALUES (:id, :name, :status)
                """), {"id": ids[name], "name": "Test organization " + name,
                        "status": status})
            for name in ("owner", "editor", "viewer", "pending", "revoked", "disabled"):
                connection.execute(text("""
                    INSERT INTO users (id, email, status, email_verified_at)
                    VALUES (:id, :email, :status, :verified)
                """), {"id": ids[name], "email": name + "@example.test",
                        "verified": None if name == "pending" else "2026-10-01T00:00:00+00:00",
                        "status": "disabled" if name == "disabled" else "active"})
            for person, org, role, status in (
                ("owner", "a", "owner", "active"),
                ("editor", "a", "editor", "active"),
                ("viewer", "a", "viewer", "active"),
                ("pending", "a", "editor", "active"),
                ("revoked", "a", "owner", "revoked"),
                ("disabled", "a", "owner", "active"),
                ("owner", "b", "viewer", "active"),
                ("owner", "suspended", "owner", "active"),
                ("owner", "legacy_locked", "owner", "active"),
            ):
                connection.execute(text("""
                    INSERT INTO organization_memberships
                    (organization_id, user_id, role, status)
                    VALUES (:org, :user, :role, :status)
                """), {
                    "org": ids[org], "user": ids[person],
                    "role": role, "status": status,
                })
            yield connection, ids
        finally:
            if transaction.is_active:
                transaction.rollback()


def _guc(connection: Connection) -> str:
    return connection.scalar(
        text("SELECT current_setting('kpdesk.organization_id', true)")
    ) or ""


def test_per_operation_membership_and_cross_organization_denial(
    tenant_connection: tuple[Connection, dict[str, uuid.UUID]],
) -> None:
    connection, ids = tenant_connection
    owner = AuthenticatedUser(ids["owner"])
    editor = AuthenticatedUser(ids["editor"])
    viewer = AuthenticatedUser(ids["viewer"])
    assert authorize_tenant_transaction(connection, owner, ids["a"], "manage").role == "owner"
    assert _guc(connection) == str(ids["a"])
    assert authorize_tenant_transaction(connection, editor, ids["a"], "write").role == "editor"
    assert authorize_tenant_transaction(connection, viewer, ids["a"], "read").role == "viewer"
    with pytest.raises(TenantAccessDenied):
        authorize_tenant_transaction(connection, viewer, ids["a"], "write")
    assert _guc(connection) == ""
    with pytest.raises(TenantAccessDenied):
        authorize_tenant_transaction(connection, viewer, ids["b"], "read")
    assert _guc(connection) == ""
    # The same user may have a DIFFERENT role in a second org, not inherited owner access.
    assert authorize_tenant_transaction(connection, owner, ids["b"], "read").role == "viewer"
    with pytest.raises(TenantAccessDenied):
        authorize_tenant_transaction(connection, owner, ids["b"], "manage")
    assert _guc(connection) == ""


def test_revoked_unverified_unknown_suspended_and_legacy_are_closed(
    tenant_connection: tuple[Connection, dict[str, uuid.UUID]],
) -> None:
    connection, ids = tenant_connection
    connection.execute(text("SELECT set_config('kpdesk.organization_id', :id, true)"),
                       {"id": str(ids["a"])})
    for who, org in (
        (ids["pending"], ids["a"]),
        (ids["revoked"], ids["a"]),
        (uuid.uuid4(), ids["a"]),
        (ids["owner"], ids["suspended"]),
        (ids["disabled"], ids["a"]),
    ):
        with pytest.raises(TenantAccessDenied):
            authorize_tenant_transaction(connection, AuthenticatedUser(who), org, "read")
        assert _guc(connection) == ""
    legacy = connection.scalar(text(
        "SELECT id FROM organizations WHERE status='legacy_locked' LIMIT 1"
    ))
    assert legacy is not None
    with pytest.raises(TenantAccessDenied):
        authorize_tenant_transaction(connection, AuthenticatedUser(ids["owner"]), legacy, "read")
    assert _guc(connection) == ""


def test_context_is_transaction_local_not_connection_persistent(
    tenant_connection: tuple[Connection, dict[str, uuid.UUID]],
) -> None:
    connection, ids = tenant_connection
    authorize_tenant_transaction(connection, AuthenticatedUser(ids["owner"]), ids["a"], "read")
    assert _guc(connection) == str(ids["a"])
    nested = connection.begin_nested()
    try:
        authorize_tenant_transaction(connection, AuthenticatedUser(ids["owner"]), ids["b"], "read")
        assert _guc(connection) == str(ids["b"])
    finally:
        nested.rollback()
    assert _guc(connection) == str(ids["a"])


def test_commit_and_rollback_clear_tenant_scope_on_reused_connection(
    tenant_connection: tuple[Connection, dict[str, uuid.UUID]],
) -> None:
    connection, ids = tenant_connection
    owner = AuthenticatedUser(ids["owner"])
    authorize_tenant_transaction(connection, owner, ids["a"], "read")
    connection.commit()  # Only synthetic rows in this test's owned disposable clone.
    assert _guc(connection) == ""
    authorize_tenant_transaction(connection, owner, ids["b"], "read")
    assert _guc(connection) == str(ids["b"])
    connection.rollback()
    assert _guc(connection) == ""


def test_string_identity_or_invalid_operation_can_never_bind_tenant(
    tenant_connection: tuple[Connection, dict[str, uuid.UUID]],
) -> None:
    connection, ids = tenant_connection
    with pytest.raises(TenantAccessDenied):
        authorize_tenant_transaction(
            connection, AuthenticatedUser(ids["owner"]), str(ids["a"]), "read",  # type: ignore[arg-type]
        )
    assert _guc(connection) == ""
    with pytest.raises(TenantAccessDenied):
        authorize_tenant_transaction(
            connection, AuthenticatedUser(ids["owner"]), ids["a"], "delete",  # type: ignore[arg-type]
        )
    assert _guc(connection) == ""
