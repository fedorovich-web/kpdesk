"""Isolated test-only auth session schema; production release not authorized.

Revision ID: 0009_auth_sessions
Revises: 0008_supplier_review_rls

Session cookies and external HTTP login are intentionally NOT enabled by this
migration. Test changes must be in a rollback-only transaction in
kpdesk_tenant_test, after temporary A3/A4 schemas.
Also accepts a temporary nonce-named, owner-comment-marked clone of
kpdesk_tenant_test used exclusively for physical-connection integration tests.
Production kpdesk is NEVER an allowed target.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

from kpdesk.db.test_database_scope import is_auth_test_database

revision = "0009_auth_sessions"
down_revision = "0008_supplier_review_rls"
branch_labels = None
depends_on = None


def _preflight(connection: sa.engine.Connection) -> None:
    if not is_auth_test_database(connection):
        raise RuntimeError("Auth migration forbidden outside kpdesk_tenant_test.")
    if connection.scalar(sa.text("SELECT version_num FROM alembic_version")) != (
        "0006_legacy_quarantine"
    ):
        raise RuntimeError("Test-only auth migration needs baseline A2 revision.")
    for table in ("catalog_versions", "supplier_reviewed_profiles"):
        status = connection.execute(sa.text("""
            SELECT relrowsecurity, relforcerowsecurity
            FROM pg_class WHERE oid=to_regclass(:table)
        """), {"table": table}).one_or_none()
        if status != (True, True):
            raise RuntimeError("Verified transaction-local A3/A4 RLS is required.")
    if connection.scalar(sa.text("SELECT current_user")) != "kpdesk":
        raise RuntimeError("Test migration must use expected PostgreSQL owner.")


def upgrade() -> None:
    _preflight(op.get_bind())
    op.create_table(
        "auth_sessions",
        sa.Column("token_sha256", sa.String(64), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("csrf_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_auth_sessions_user",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "token_sha256 ~ '^[0-9a-f]{64}$' "
            "AND csrf_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_auth_session_sha256",
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name="ck_auth_session_valid_lifetime",
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="ck_auth_session_revocation_time",
        ),
    )
    op.create_index(
        "ix_auth_sessions_user_active", "auth_sessions",
        ["user_id", "expires_at"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    # Explicitly no broad table grants here: production auth privileges must be
    # allocated through a reviewed least-privilege boundary. Test grants are
    # scoped to the outer PostgreSQL transaction and rolled back.


def downgrade() -> None:
    raise RuntimeError("Auth test migration must roll back as a whole transaction.")
