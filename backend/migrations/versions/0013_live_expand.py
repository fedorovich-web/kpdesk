"""Production-compatible additive multi-tenant release foundation.

Revision ID: 0013_live_expand
Revises: 0005_tenant_expand

Unlike test-only 0006-0012, this is an ACTUAL local-release branch from 0005.
It preserves every legacy table, PK, active singleton and owner-token API.
No public tenant session routes are activated by this migration.

Only supports the VERIFIED EMPTY legacy installation; never claims to backfill
unreviewed historical data. Probes are performed before all DDL.
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0013_live_expand"
down_revision = "0005_tenant_expand"
branch_labels = ("live_release",)
depends_on = None

LEGACY_TABLES = (
    "catalog_versions", "catalog_items", "idempotency_records",
    "quote_documents", "quote_revisions", "quote_write_requests",
)
_TENANT_FK = (
    ("catalog_items", "fk_live_item_scoped_catalog",
     ("organization_id", "catalog_version_id"), "catalog_versions",
     ("organization_id", "id")),
    ("idempotency_records", "fk_live_import_scoped_catalog",
     ("organization_id", "result_version_id"), "catalog_versions",
     ("organization_id", "id")),
    ("quote_revisions", "fk_live_revision_scoped_document",
     ("organization_id", "quote_id"), "quote_documents",
     ("organization_id", "id")),
    ("quote_revisions", "fk_live_revision_scoped_catalog",
     ("organization_id", "catalog_version_id"), "catalog_versions",
     ("organization_id", "id")),
    ("quote_write_requests", "fk_live_quote_write_scoped_document",
     ("organization_id", "quote_id"), "quote_documents",
     ("organization_id", "id")),
)
_PARENTS = (
    ("catalog_versions", "uq_live_catalog_org_id", ("organization_id", "id")),
    ("quote_documents", "uq_live_document_org_id", ("organization_id", "id")),
    ("quote_revisions", "uq_live_revision_org_key",
     ("organization_id", "quote_id", "number")),
)
_CLONE = re.compile(r"kpdesk_live_expand_[0-9a-f]{12}\Z")


def _preflight(connection: sa.engine.Connection) -> None:
    name = connection.scalar(sa.text("SELECT current_database()"))
    if name != "kpdesk":
        if not isinstance(name, str) or not _CLONE.fullmatch(name):
            raise RuntimeError("Live expansion is restricted to KPDesk or a marked rehearsal clone.")
        mark = connection.scalar(sa.text("""
            SELECT shobj_description(oid, 'pg_database')
            FROM pg_database WHERE datname=:name
        """), {"name": name})
        if mark != "kpdesk:live-expand-rehearsal:" + name.rsplit("_", 1)[-1]:
            raise RuntimeError("Clone provenance marker invalid.")
    if connection.scalar(sa.text("SELECT current_user")) != "kpdesk":
        raise RuntimeError("Only the verified local database owner may migrate.")
    if connection.scalar(sa.text("SELECT version_num FROM alembic_version")) != (
        "0005_tenant_expand"
    ):
        raise RuntimeError("Live expansion requires Alembic 0005 baseline.")
    if connection.scalar(sa.text("""
        SELECT rolsuper FROM pg_roles WHERE rolname='kpdesk_app'
    """)) is not False:
        raise RuntimeError("Application database role must remain unprivileged.")
    if connection.scalar(sa.text("""
        SELECT count(*) FROM catalog_active WHERE id<>1 OR catalog_version_id IS NOT NULL
    """)) != 0 or connection.scalar(sa.text("SELECT count(*) FROM catalog_active")) != 1:
        raise RuntimeError("Existing singleton catalog must be empty.")
    for table in (*LEGACY_TABLES, "users", "organizations", "organization_memberships"):
        if connection.scalar(sa.text(f"SELECT count(*) FROM public.{table}")):
            raise RuntimeError(
                f"Live expansion cannot silently reassign existing records: {table}"
            )
    if connection.scalar(sa.text("""
        SELECT tgenabled FROM pg_trigger
        WHERE tgrelid='public.quote_revisions'::regclass
          AND tgname='quote_revision_no_update_delete' AND NOT tgisinternal
    """)) != "O":
        raise RuntimeError("Quote history immutability trigger missing.")
    if connection.scalar(sa.text("SELECT to_regclass('public.tenant_catalog_active')")):
        raise RuntimeError("Tenant release tables already exist.")


def upgrade() -> None:
    _preflight(op.get_bind())

    # Add same-org reference keys without touching legacy globally unique IDs.
    for table, name, columns in _PARENTS:
        op.create_unique_constraint(name, table, list(columns))
    for table, name, columns, parent, ref in _TENANT_FK:
        op.create_foreign_key(
            name, table, parent, list(columns), list(ref), ondelete="RESTRICT",
        )

    # Separate organization pointers/keys: the legacy catalog_active(id=1)
    # and owner-token API remain unchanged until the subsequent cutover.
    op.create_table(
        "tenant_catalog_active",
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("catalog_version_id", UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("organization_id", name="pk_tenant_catalog_active"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_live_active_org", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "catalog_version_id"],
            ["catalog_versions.organization_id", "catalog_versions.id"],
            name="fk_live_active_scoped_catalog", ondelete="RESTRICT",
        ),
    )
    op.create_table(
        "tenant_catalog_idempotency",
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("namespace", sa.String(60), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("result_version_id", UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "organization_id", "namespace", "key",
            name="pk_tenant_catalog_idempotency",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "result_version_id"],
            ["catalog_versions.organization_id", "catalog_versions.id"],
            name="fk_live_idempotency_scoped_catalog", ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "request_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_live_catalog_idempotency_sha",
        ),
    )
    op.create_table(
        "tenant_quote_write_requests",
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("quote_id", UUID(as_uuid=True), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint(
            "organization_id", "key", name="pk_tenant_quote_write_requests",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "quote_id", "revision_number"],
            ["quote_revisions.organization_id", "quote_revisions.quote_id",
             "quote_revisions.number"],
            name="fk_live_idempotency_scoped_revision", ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "revision_number >= 1", name="ck_live_quote_request_revision",
        ),
        sa.CheckConstraint(
            "request_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_live_quote_idempotency_sha",
        ),
    )
    op.create_index(
        "ix_live_catalog_org_created", "catalog_versions",
        ["organization_id", "created_at"],
    )
    op.create_index(
        "ix_live_quote_org_created", "quote_documents",
        ["organization_id", "created_at"],
    )
    # Never grant broad access to the new tables until session-bound RLS
    # and login/CSRF permissions have their separate migration and review.


def downgrade() -> None:
    conn = op.get_bind()
    if conn.scalar(sa.text("SELECT current_database()")) != "kpdesk" and (
        not isinstance(conn.scalar(sa.text("SELECT current_database()")), str)
        or not _CLONE.fullmatch(conn.scalar(sa.text("SELECT current_database()")))
    ):
        raise RuntimeError("Unsupported database for live release downgrade.")
    for table in ("tenant_catalog_active", "tenant_catalog_idempotency",
                  "tenant_quote_write_requests"):
        if conn.scalar(sa.text(f"SELECT count(*) FROM {table}")):
            raise RuntimeError("Cannot erase live tenant records during downgrade.")
    for table in LEGACY_TABLES:
        if conn.scalar(sa.text(
            f"SELECT count(*) FROM {table} WHERE organization_id IS NOT NULL"
        )):
            raise RuntimeError("Organization-owned data blocks automatic downgrade.")
    for table, name in (
        ("catalog_versions", "ix_live_catalog_org_created"),
        ("quote_documents", "ix_live_quote_org_created"),
    ):
        op.drop_index(name, table_name=table)
    for table in (
        "tenant_quote_write_requests", "tenant_catalog_idempotency",
        "tenant_catalog_active",
    ):
        op.drop_table(table)
    for table, name, *_ in reversed(_TENANT_FK):
        op.drop_constraint(name, table, type_="foreignkey")
    for table, name, _ in reversed(_PARENTS):
        op.drop_constraint(name, table, type_="unique")
