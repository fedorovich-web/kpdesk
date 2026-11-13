"""Add-only tenant foundation; no backfill, RLS, or production data changes.

Revision ID: 0005_tenant_expand
Revises: 0004_quote_revisions
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0005_tenant_expand"
down_revision = "0004_quote_revisions"
branch_labels = None
depends_on = None

_DOMAIN_TABLES = (
    "catalog_versions",
    "catalog_items",
    "catalog_active",
    "idempotency_records",
    "quote_documents",
    "quote_revisions",
    "quote_write_requests",
)


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_organization_name"),
        sa.CheckConstraint(
            "status IN ('active','legacy_locked','suspended')",
            name="ck_organization_status",
        ),
    )
    op.create_table(
        "users",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("password_hash", sa.String(512), nullable=True),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.CheckConstraint("email = lower(trim(email))", name="ck_user_normalized_email"),
        sa.CheckConstraint(
            "status IN ('pending','active','disabled')", name="ck_user_status",
        ),
    )
    op.create_index("uq_users_email_normalized", "users", ["email"], unique=True)
    op.create_table(
        "organization_memberships",
        sa.Column(
            "organization_id", UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"), primary_key=True,
        ),
        sa.Column(
            "user_id", UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('owner','editor','viewer')", name="ck_membership_role",
        ),
        sa.CheckConstraint(
            "status IN ('active','revoked')", name="ck_membership_status",
        ),
    )
    op.create_index(
        "ix_organization_memberships_user_id",
        "organization_memberships", ["user_id"],
    )
    for table in _DOMAIN_TABLES:
        op.add_column(table, sa.Column("organization_id", UUID(as_uuid=True), nullable=True))
        op.create_foreign_key(
            f"fk_{table}_organization_id", table, "organizations",
            ["organization_id"], ["id"], ondelete="RESTRICT",
        )
        op.create_index(f"ix_{table}_organization_id", table, ["organization_id"])


def downgrade() -> None:
    connection = op.get_bind()
    for table in _DOMAIN_TABLES:
        count = connection.scalar(sa.text(
            f"SELECT count(*) FROM {table} WHERE organization_id IS NOT NULL"
        ))
        if count:
            raise RuntimeError(f"Cannot drop tenant data from {table}")
    for table in ("organization_memberships", "users", "organizations"):
        count = connection.scalar(sa.text(f"SELECT count(*) FROM {table}"))
        if count:
            raise RuntimeError(f"Cannot drop nonempty tenant table {table}")
    for table in reversed(_DOMAIN_TABLES):
        op.drop_index(f"ix_{table}_organization_id", table_name=table)
        op.drop_constraint(f"fk_{table}_organization_id", table, type_="foreignkey")
        op.drop_column(table, "organization_id")
    op.drop_index("ix_organization_memberships_user_id", table_name="organization_memberships")
    op.drop_table("organization_memberships")
    op.drop_index("uq_users_email_normalized", table_name="users")
    op.drop_table("users")
    op.drop_table("organizations")
