"""Test-only A4 PostgreSQL supplier review evidence; append-only, FORCE RLS.

Revision ID: 0008_supplier_review_rls
Revises: 0007_tenant_contract_rls

PRODUCTION MIGRATION NOT AUTHORIZED. Run ONLY as DDL inside a rollback-only
transaction on kpdesk_tenant_test AFTER verified A3 test-only contract DDL.
A real release requires a separate reviewed migration and verified backup.
Also accepts a temporary nonce-named, owner-comment-marked clone of
kpdesk_tenant_test used exclusively for physical-connection integration tests.
Production kpdesk is NEVER an allowed target.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

from kpdesk.db.test_database_scope import is_auth_test_database

revision = "0008_supplier_review_rls"
down_revision = "0007_tenant_contract_rls"
branch_labels = None
depends_on = None

REVIEW_TABLES = ("supplier_reviewed_profiles", "supplier_review_decisions")


def _preflight(connection: sa.engine.Connection) -> None:
    if not is_auth_test_database(connection):
        raise RuntimeError("A4 review migration forbidden outside kpdesk_tenant_test.")
    # Manual transactional A3 verification intentionally does not stamp Alembic.
    if connection.scalar(sa.text("SELECT version_num FROM alembic_version")) != (
        "0006_legacy_quarantine"
    ):
        raise RuntimeError("A4 review test requires unchanged A2 revision.")
    guards = connection.execute(sa.text("""
        SELECT relrowsecurity, relforcerowsecurity
        FROM pg_class WHERE oid='catalog_versions'::regclass
    """)).one()
    if guards != (True, True):
        raise RuntimeError("Verified transaction-local A3 RLS contract required.")
    user = connection.scalar(sa.text("SELECT current_user"))
    if user != "kpdesk":
        raise RuntimeError("Test migration requires the restricted test DB owner.")


def upgrade() -> None:
    _preflight(op.get_bind())
    op.create_table(
        "supplier_reviewed_profiles",
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("supplier_id", sa.String(120), nullable=False),
        sa.Column("schema_fingerprint", sa.String(64), nullable=False),
        sa.Column("profile_sha256", sa.String(64), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("approved_by", UUID(as_uuid=True), nullable=False),
        sa.Column("profile_json", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint(
            "organization_id", "supplier_id", "schema_fingerprint", "profile_sha256",
            name="pk_reviewed_supplier_profiles",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="RESTRICT",
            name="fk_supplier_profile_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by"], ["users.id"], ondelete="RESTRICT",
            name="fk_supplier_profile_reviewer",
        ),
        sa.CheckConstraint(
            "supplier_id ~ '^[A-Za-z0-9._-]{1,120}$' "
            "AND supplier_id NOT IN ('.','..') "
            "AND position('..' in supplier_id) = 0",
            name="ck_supplier_profile_identifier",
        ),
        sa.CheckConstraint(
            "schema_fingerprint ~ '^[0-9a-f]{64}$' "
            "AND profile_sha256 ~ '^[0-9a-f]{64}$' "
            "AND source_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_supplier_profile_sha256",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(profile_json) = 'object'",
            name="ck_supplier_profile_json_object",
        ),
    )
    op.create_table(
        "supplier_review_decisions",
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("supplier_id", sa.String(120), nullable=False),
        sa.Column("schema_fingerprint", sa.String(64), nullable=False),
        sa.Column("profile_sha256", sa.String(64), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("source_line", sa.Integer(), nullable=False),
        sa.Column("source_row_sha256", sa.String(64), nullable=False),
        sa.Column("decision_kind", sa.String(16), nullable=False),
        sa.Column("decision_json", JSONB(), nullable=False),
        sa.Column("reviewed_by", UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint(
            "organization_id", "supplier_id", "source_sha256", "source_line",
            name="pk_supplier_review_source_row",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "supplier_id", "schema_fingerprint", "profile_sha256"],
            [
                "supplier_reviewed_profiles.organization_id",
                "supplier_reviewed_profiles.supplier_id",
                "supplier_reviewed_profiles.schema_fingerprint",
                "supplier_reviewed_profiles.profile_sha256",
            ],
            ondelete="RESTRICT", name="fk_review_decisions_tenant_profile",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by"], ["users.id"], ondelete="RESTRICT",
            name="fk_review_decisions_reviewer",
        ),
        sa.CheckConstraint("source_line > 0", name="ck_review_source_line"),
        sa.CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$' "
            "AND source_row_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_review_evidence_hashes",
        ),
        sa.CheckConstraint(
            "decision_kind IN ('accepted','excluded')",
            name="ck_review_decision_kind",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(decision_json) = 'object'",
            name="ck_review_decision_json_object",
        ),
        sa.CheckConstraint(
            "decision_kind <> 'excluded' OR "
            "(decision_json ? 'reason' AND "
            "length(trim(decision_json ->> 'reason')) >= 8)",
            name="ck_review_exclusion_reason",
        ),
    )
    op.create_index(
        "ix_supplier_profiles_tenant_supplier_created",
        "supplier_reviewed_profiles",
        ["organization_id", "supplier_id", "created_at"],
    )
    op.create_index(
        "ix_supplier_reviews_tenant_profile_created",
        "supplier_review_decisions",
        ["organization_id", "supplier_id", "profile_sha256", "created_at"],
    )
    for table in REVIEW_TABLES:
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
        "A4 supplier review downgrade prohibited; roll back the whole test transaction."
    )
