"""Atomically quarantine the pre-tenant catalog and quotes without user membership.

Revision ID: 0006_legacy_quarantine
Revises: 0005_tenant_expand

Safety: initially test-database-only; no production invocation. This migration
temporarily disables the immutable-quote trigger INSIDE an exclusive-locking,
transactional Alembic migration solely to populate organization_id; the trigger
is re-enabled before commit and payload hashes must match byte-for-byte.
"""

from __future__ import annotations

import hashlib
import json
import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.engine import Connection

from kpdesk.db.test_database_scope import is_auth_test_database

revision = "0006_legacy_quarantine"
down_revision = "0005_tenant_expand"
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
_LOCK_TABLES = (*_DOMAIN_TABLES, "organizations", "organization_memberships",
                "legacy_quarantine_audit")
_QUARANTINE_ID = uuid.uuid5(uuid.NAMESPACE_URL, "https://kpdesk.ru/legacy-locked")
_QUARANTINE_NAME = "KPDesk legacy quarantine"
_IMMUTABLE_TRIGGER = "quote_revision_no_update_delete"

_ORPHAN_QUERIES = (
    """SELECT count(*) FROM catalog_items c
       LEFT JOIN catalog_versions v ON v.id = c.catalog_version_id WHERE v.id IS NULL""",
    """SELECT count(*) FROM catalog_active a
       LEFT JOIN catalog_versions v ON v.id = a.catalog_version_id
       WHERE a.catalog_version_id IS NOT NULL AND v.id IS NULL""",
    """SELECT count(*) FROM idempotency_records i
       LEFT JOIN catalog_versions v ON v.id = i.result_version_id WHERE v.id IS NULL""",
    """SELECT count(*) FROM quote_revisions r
       LEFT JOIN quote_documents q ON q.id = r.quote_id
       LEFT JOIN catalog_versions v ON v.id = r.catalog_version_id
       WHERE q.id IS NULL OR v.id IS NULL""",
    """SELECT count(*) FROM quote_write_requests w
       LEFT JOIN quote_revisions r ON r.quote_id = w.quote_id
                                     AND r.number = w.revision_number
       WHERE r.quote_id IS NULL""",
)


def _fingerprint(connection: Connection, *, snapshots_only: bool) -> str:
    """SHA256 over stable JSONB representations, excluding only tenant metadata."""
    hasher = hashlib.sha256()
    for table in ("quote_revisions",) if snapshots_only else _DOMAIN_TABLES:
        hasher.update(table.encode("ascii") + b"\x00")
        if snapshots_only:
            statement = sa.text("""
                SELECT jsonb_build_object(
                    'quote_id', quote_id, 'number', number,
                    'snapshot', snapshot, 'snapshot_sha256', snapshot_sha256
                )::text AS payload
                FROM quote_revisions ORDER BY quote_id, number
            """)
        else:
            statement = sa.text(
                f"SELECT (to_jsonb(t) - 'organization_id')::text AS payload "
                f"FROM {table} t ORDER BY payload"
            )
        for (payload,) in connection.execute(statement):
            encoded = payload.encode("utf-8")
            hasher.update(len(encoded).to_bytes(8, "big"))
            hasher.update(encoded)
    return hasher.hexdigest()


def _counts(connection: Connection) -> dict[str, int]:
    return {
        table: int(connection.scalar(sa.text(f"SELECT count(*) FROM {table}")) or 0)
        for table in _DOMAIN_TABLES
    }


def _ensure_trigger_enabled(connection: Connection) -> None:
    state = connection.scalar(sa.text(
        "SELECT tgenabled FROM pg_trigger "
        "WHERE tgrelid = 'quote_revisions'::regclass "
        "AND tgname = :name AND NOT tgisinternal"
    ), {"name": _IMMUTABLE_TRIGGER})
    if state != "O":
        raise RuntimeError("Quote revision immutability trigger is not enabled.")


def quarantine_legacy_rows(connection: Connection) -> uuid.UUID:
    """Same-transaction, fail-closed, idempotent quarantine; migration owns commit."""
    if (
        connection.scalar(sa.text("SELECT current_database()")) != "kpdesk_test"
        and not is_auth_test_database(connection)
    ):
        raise RuntimeError("A2 is restricted to local test databases until approved rollout.")
    if not connection.in_transaction():
        raise RuntimeError("A2 requires an active transaction.")

    # Serialize against all concurrent readers/writers during the one-time transition.
    for table in _LOCK_TABLES:
        connection.execute(sa.text(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE"))

    _ensure_trigger_enabled(connection)
    audit = connection.execute(sa.text("""
        SELECT organization_id, counts_after, rows_sha256_after,
               snapshots_sha256_after
        FROM legacy_quarantine_audit
    """)).mappings().all()
    if len(audit) > 1:
        raise RuntimeError("Multiple legacy quarantine audits are not supported.")

    if audit:
        saved = audit[0]
        if saved["organization_id"] != _QUARANTINE_ID:
            raise RuntimeError("Legacy audit references a different quarantine.")
        for table in _DOMAIN_TABLES:
            if connection.scalar(sa.text(
                f"SELECT count(*) FROM {table} WHERE organization_id IS NULL "
                "OR organization_id != :org"
            ), {"org": _QUARANTINE_ID}):
                raise RuntimeError(f"New unscoped or foreign organization rows in {table}.")
        status = connection.scalar(sa.text(
            "SELECT status FROM organizations WHERE id = :id"
        ), {"id": _QUARANTINE_ID})
        if status != "legacy_locked":
            raise RuntimeError("Legacy quarantine organization is not locked.")
        if connection.scalar(sa.text(
            "SELECT count(*) FROM organization_memberships WHERE organization_id = :id"
        ), {"id": _QUARANTINE_ID}):
            raise RuntimeError("Legacy quarantine has an unauthorized membership.")
        if (
            saved["counts_after"] != _counts(connection)
            or saved["rows_sha256_after"] != _fingerprint(
                connection, snapshots_only=False,
            )
            or saved["snapshots_sha256_after"] != _fingerprint(
                connection, snapshots_only=True,
            )
        ):
            raise RuntimeError("Quarantined data changed since the audit.")
        return _QUARANTINE_ID

    # Never sweep data belonging to a partially configured tenant into legacy.
    if connection.scalar(sa.text("SELECT count(*) FROM organizations")):
        raise RuntimeError("Existing organizations require a separately reviewed migration.")
    for table in _DOMAIN_TABLES:
        if connection.scalar(sa.text(
            f"SELECT count(*) FROM {table} WHERE organization_id IS NOT NULL"
        )):
            raise RuntimeError(f"Unexpected organization assignment in {table}.")
    for orphan in _ORPHAN_QUERIES:
        if connection.scalar(sa.text(orphan)):
            raise RuntimeError("Legacy cross-table references are not intact.")

    before_counts = _counts(connection)
    before_rows = _fingerprint(connection, snapshots_only=False)
    before_snapshots = _fingerprint(connection, snapshots_only=True)

    connection.execute(sa.text("""
        INSERT INTO organizations (id, name, status)
        VALUES (:id, :name, 'legacy_locked')
    """), {"id": _QUARANTINE_ID, "name": _QUARANTINE_NAME})

    for table in _DOMAIN_TABLES:
        if table == "quote_revisions":
            # DROP/DISABLE is transactional and never visible after rollback.
            # ACCESS EXCLUSIVE locks prevent concurrent writes while relaxed.
            connection.execute(sa.text(
                "ALTER TABLE quote_revisions "
                "DISABLE TRIGGER quote_revision_no_update_delete"
            ))
            try:
                connection.execute(sa.text("""
                    UPDATE quote_revisions SET organization_id = :id
                    WHERE organization_id IS NULL
                """), {"id": _QUARANTINE_ID})
            finally:
                connection.execute(sa.text(
                    "ALTER TABLE quote_revisions "
                    "ENABLE TRIGGER quote_revision_no_update_delete"
                ))
        else:
            connection.execute(sa.text(
                f"UPDATE {table} SET organization_id = :id WHERE organization_id IS NULL"
            ), {"id": _QUARANTINE_ID})
    _ensure_trigger_enabled(connection)
    after_counts = _counts(connection)
    after_rows = _fingerprint(connection, snapshots_only=False)
    after_snapshots = _fingerprint(connection, snapshots_only=True)
    if (after_counts != before_counts or after_rows != before_rows
            or after_snapshots != before_snapshots):
        raise RuntimeError("A2 changed legacy payload or row count; transaction must roll back.")
    for table in _DOMAIN_TABLES:
        if connection.scalar(sa.text(
            f"SELECT count(*) FROM {table} WHERE organization_id IS NULL "
            "OR organization_id != :org"
        ), {"org": _QUARANTINE_ID}):
            raise RuntimeError(f"Backfill incomplete in {table}.")
    connection.execute(sa.text("""
        INSERT INTO legacy_quarantine_audit (
            organization_id, counts_before, counts_after,
            rows_sha256_before, rows_sha256_after,
            snapshots_sha256_before, snapshots_sha256_after
        ) VALUES (
            :id, CAST(:before_counts AS jsonb), CAST(:after_counts AS jsonb),
            :before_rows, :after_rows, :before_snapshots, :after_snapshots
        )
    """), {
        "id": _QUARANTINE_ID,
        "before_counts": json.dumps(before_counts, sort_keys=True),
        "after_counts": json.dumps(after_counts, sort_keys=True),
        "before_rows": before_rows,
        "after_rows": after_rows,
        "before_snapshots": before_snapshots,
        "after_snapshots": after_snapshots,
    })
    return _QUARANTINE_ID


def upgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT current_database()")) not in (
        "kpdesk_test", "kpdesk_tenant_test",
    ):
        raise RuntimeError("A2 can only run against local test databases until approval.")
    op.create_table(
        "legacy_quarantine_audit",
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey(
            "organizations.id", ondelete="RESTRICT",
        ), primary_key=True),
        sa.Column("counts_before", JSONB(), nullable=False),
        sa.Column("counts_after", JSONB(), nullable=False),
        sa.Column("rows_sha256_before", sa.String(64), nullable=False),
        sa.Column("rows_sha256_after", sa.String(64), nullable=False),
        sa.Column("snapshots_sha256_before", sa.String(64), nullable=False),
        sa.Column("snapshots_sha256_after", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.CheckConstraint("counts_before = counts_after",
                           name="ck_legacy_quarantine_same_counts"),
        sa.CheckConstraint("rows_sha256_before = rows_sha256_after",
                           name="ck_legacy_quarantine_same_rows"),
        sa.CheckConstraint("snapshots_sha256_before = snapshots_sha256_after",
                           name="ck_legacy_quarantine_same_snapshots"),
    )
    op.execute("""
        CREATE FUNCTION kpdesk_no_legacy_audit_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Legacy quarantine audit is immutable';
        END;
        $$;
    """)
    op.execute("""
        CREATE TRIGGER legacy_quarantine_audit_no_update_delete
        BEFORE UPDATE OR DELETE ON legacy_quarantine_audit
        FOR EACH ROW EXECUTE FUNCTION kpdesk_no_legacy_audit_mutation();
    """)
    quarantine_legacy_rows(op.get_bind())


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT count(*) FROM legacy_quarantine_audit")):
        raise RuntimeError("Cannot downgrade verified legacy quarantine with data.")
    op.execute("DROP TRIGGER legacy_quarantine_audit_no_update_delete ON legacy_quarantine_audit")
    op.execute("DROP FUNCTION kpdesk_no_legacy_audit_mutation()")
    op.drop_table("legacy_quarantine_audit")
