"""Immutable quote snapshots and optimistic revision pointer.

Revision ID: 0004_quote_revisions
Revises: 0003_broad_catalog
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0004_quote_revisions"
down_revision = "0003_broad_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quote_documents",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("current_revision >= 1", name="positive_quote_revision"),
    )
    op.create_table(
        "quote_revisions",
        sa.Column("quote_id", UUID(as_uuid=True), sa.ForeignKey("quote_documents.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("number", sa.Integer(), primary_key=True),
        sa.Column("catalog_version_id", UUID(as_uuid=True), sa.ForeignKey("catalog_versions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("snapshot", JSONB(), nullable=False),
        sa.Column("snapshot_sha256", sa.String(length=64), nullable=False),
        sa.Column("is_complete", sa.Boolean(), nullable=False),
        sa.Column("total_rub", sa.Numeric(14, 2), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("number >= 1", name="positive_quote_revision_number"),
        sa.CheckConstraint(
            "(is_complete AND total_rub IS NOT NULL) OR "
            "(NOT is_complete AND total_rub IS NULL)",
            name="quote_completion_total_consistent",
        ),
        sa.CheckConstraint("total_rub IS NULL OR total_rub >= 0", name="quote_total_nonnegative"),
    )
    op.create_table(
        "quote_write_requests",
        sa.Column("key", sa.String(length=128), primary_key=True),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("quote_id", UUID(as_uuid=True), sa.ForeignKey("quote_documents.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["quote_id", "revision_number"], ["quote_revisions.quote_id", "quote_revisions.number"],
            name="fk_quote_write_revision", ondelete="RESTRICT",
        ),
    )
    # Protect immutable history from application mistakes and stale transactions.
    op.execute("""
        CREATE FUNCTION kpdesk_no_quote_revision_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Historical quote revisions are immutable';
        END;
        $$;
    """)
    op.execute("""
        CREATE TRIGGER quote_revision_no_update_delete
        BEFORE UPDATE OR DELETE ON quote_revisions
        FOR EACH ROW EXECUTE FUNCTION kpdesk_no_quote_revision_mutation();
    """)


def downgrade() -> None:
    # Destructive rollback is disallowed once quotes exist.
    count = op.get_bind().execute(sa.text("SELECT count(*) FROM quote_documents")).scalar_one()
    if count:
        raise RuntimeError("Cannot downgrade while quote documents exist")
    op.execute("DROP TRIGGER IF EXISTS quote_revision_no_update_delete ON quote_revisions")
    op.execute("DROP FUNCTION IF EXISTS kpdesk_no_quote_revision_mutation()")
    op.drop_table("quote_write_requests")
    op.drop_table("quote_revisions")
    op.drop_table("quote_documents")
