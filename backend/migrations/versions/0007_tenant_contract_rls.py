"""Test-only A3 tenant contract: composite FK, scoped keys and FORCE RLS.

Revision ID: 0007_tenant_contract_rls
Revises: 0006_legacy_quarantine

PRODUCTION DEPLOYMENT NOT AUTHORIZED. The migration intentionally refuses every
database other than the dedicated kpdesk_tenant_test. No automatic downgrade;
a reviewed release migration/backups/rollout are required for production.
Also accepts a temporary nonce-named, owner-comment-marked clone of
kpdesk_tenant_test used exclusively for physical-connection integration tests.
Production kpdesk is NEVER an allowed target.
"""
from __future__ import annotations

import importlib

import sqlalchemy as sa
from alembic import op

from kpdesk.db.test_database_scope import is_auth_test_database

revision = "0007_tenant_contract_rls"
down_revision = "0006_legacy_quarantine"
branch_labels = None
depends_on = None

DOMAIN_TABLES = (
    "catalog_versions", "catalog_items", "catalog_active",
    "idempotency_records", "quote_documents", "quote_revisions",
    "quote_write_requests",
)
_PARENT_UNIQUES = (
    ("catalog_versions", "uq_catalog_versions_org_id", ("organization_id", "id")),
    ("quote_documents", "uq_quote_documents_org_id", ("organization_id", "id")),
    ("quote_revisions", "uq_quote_revisions_org_quote_number",
     ("organization_id", "quote_id", "number")),
)
_FKS = (
    ("catalog_items", "fk_catalog_items_tenant_catalog",
     ("organization_id", "catalog_version_id"), "catalog_versions",
     ("organization_id", "id")),
    ("catalog_active", "fk_catalog_active_tenant_catalog",
     ("organization_id", "catalog_version_id"), "catalog_versions",
     ("organization_id", "id")),
    ("idempotency_records", "fk_idempotency_tenant_catalog",
     ("organization_id", "result_version_id"), "catalog_versions",
     ("organization_id", "id")),
    ("quote_revisions", "fk_quote_revisions_tenant_document",
     ("organization_id", "quote_id"), "quote_documents",
     ("organization_id", "id")),
    ("quote_revisions", "fk_quote_revisions_tenant_catalog",
     ("organization_id", "catalog_version_id"), "catalog_versions",
     ("organization_id", "id")),
    ("quote_write_requests", "fk_quote_writes_tenant_document",
     ("organization_id", "quote_id"), "quote_documents",
     ("organization_id", "id")),
    ("quote_write_requests", "fk_quote_writes_tenant_revision",
     ("organization_id", "quote_id", "revision_number"), "quote_revisions",
     ("organization_id", "quote_id", "number")),
)


def _preflight(connection: sa.engine.Connection) -> None:
    if not is_auth_test_database(connection):
        raise RuntimeError("A3 migration forbidden outside kpdesk_tenant_test.")
    if connection.scalar(sa.text("SELECT version_num FROM alembic_version")) != (
        "0006_legacy_quarantine"
    ):
        raise RuntimeError("A3 requires verified A2 migration state.")
    if connection.scalar(sa.text("SELECT count(*) FROM legacy_quarantine_audit")) != 1:
        raise RuntimeError("A2 legacy quarantine audit not verified.")
    if connection.scalar(sa.text("""
        SELECT count(*) FROM legacy_quarantine_audit
        WHERE counts_before = counts_after
          AND rows_sha256_before = rows_sha256_after
          AND snapshots_sha256_before = snapshots_sha256_after
    """)) != 1:
        raise RuntimeError("A2 legacy audit consistency mismatch.")
    if connection.scalar(sa.text("""
        SELECT count(*) FROM organization_memberships AS m
        JOIN organizations AS o ON o.id=m.organization_id
        WHERE o.status='legacy_locked'
    """)):
        raise RuntimeError("Legacy organization must not have web memberships.")
    for table in DOMAIN_TABLES:
        if connection.scalar(sa.text(
            f"SELECT count(*) FROM {table} WHERE organization_id IS NULL"
        )):
            raise RuntimeError(f"Unscoped {table} rows: refusing tenant contract.")
    role = connection.execute(sa.text("""
        SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole
        FROM pg_roles WHERE rolname='kpdesk_app'
    """)).one_or_none()
    if role != (False, False, False, False):
        raise RuntimeError("Runtime PostgreSQL role privileges are unsafe.")
    # Re-run the A2 immutable payload and row fingerprint verification on the
    # quarantined original rows before any A3 DDL. A saved hash alone is not proof.
    a2 = importlib.import_module("migrations.versions.0006_legacy_quarantine")
    a2.quarantine_legacy_rows(connection)


def upgrade() -> None:
    _preflight(op.get_bind())
    for table in DOMAIN_TABLES:
        op.alter_column(table, "organization_id", existing_type=sa.UUID(), nullable=False)
    for table, name, columns in _PARENT_UNIQUES:
        op.create_unique_constraint(name, table, list(columns))

    # Old global keys must no longer reject identical idempotency keys in two
    # different organizations. UUID identity and immutable snapshots are intact.
    op.drop_constraint("idempotency_records_pkey", "idempotency_records", type_="primary")
    op.create_primary_key(
        "idempotency_records_pkey", "idempotency_records",
        ["organization_id", "namespace", "key"],
    )
    op.drop_constraint("quote_write_requests_pkey", "quote_write_requests",
                       type_="primary")
    op.create_primary_key(
        "quote_write_requests_pkey", "quote_write_requests",
        ["organization_id", "key"],
    )

    # Replace the singleton pointer without repointing any version UUID.
    op.drop_constraint("singleton_active_catalog", "catalog_active", type_="check")
    op.drop_constraint("catalog_active_pkey", "catalog_active", type_="primary")
    op.drop_column("catalog_active", "id")
    op.create_primary_key(
        "catalog_active_pkey", "catalog_active", ["organization_id"],
    )

    # Eliminate global-only references: every child now checks its
    # organization_id against the same organization as the parent record.
    for table, old_name in (
        ("catalog_items", "catalog_items_catalog_version_id_fkey"),
        ("catalog_active", "catalog_active_catalog_version_id_fkey"),
        ("idempotency_records", "idempotency_records_result_version_id_fkey"),
        ("quote_revisions", "quote_revisions_quote_id_fkey"),
        ("quote_revisions", "quote_revisions_catalog_version_id_fkey"),
        ("quote_write_requests", "quote_write_requests_quote_id_fkey"),
        ("quote_write_requests", "fk_quote_write_revision"),
    ):
        op.drop_constraint(old_name, table, type_="foreignkey")

    for table, name, columns, parent, parent_columns in _FKS:
        op.create_foreign_key(
            name, table, parent, list(columns), list(parent_columns),
            ondelete="RESTRICT",
        )
    op.create_index(
        "ix_catalog_versions_org_created", "catalog_versions",
        ["organization_id", "created_at"],
    )
    op.create_index(
        "ix_quote_documents_org_created", "quote_documents",
        ["organization_id", "created_at"],
    )

    # Defense in depth, NOT user authentication. The server must verify an
    # active membership and bind a transaction-local org before accessing data.
    for table in DOMAIN_TABLES:
        predicate = (
            "organization_id = "
            "NULLIF(current_setting('kpdesk.organization_id', true), '')::uuid"
        )
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_access_{table} ON {table} FOR ALL TO kpdesk_app "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )


def downgrade() -> None:
    raise RuntimeError(
        "A3 destructive downgrade prohibited; restore an independently verified "
        "test backup or roll back the surrounding test transaction."
    )
