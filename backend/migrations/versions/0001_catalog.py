"""Initial immutable catalog, active pointer and idempotency.

Revision ID: 0001_catalog
Revises:
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_catalog"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "catalog_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("original_filename", sa.String(512), nullable=False),
        sa.Column("rows_count", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("rows_count > 0", name="positive_catalog_rows"),
    )
    op.create_table(
        "catalog_items",
        sa.Column("catalog_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sku", sa.String(100), nullable=False),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("category", sa.String(10), nullable=False),
        sa.Column("sale_unit", sa.String(20), nullable=False),
        sa.Column("pieces_per_sale_unit", sa.Integer, nullable=False),
        sa.Column("price_rub", sa.Numeric(14, 2), nullable=False),
        sa.Column("brand", sa.String(200)),
        sa.Column("paper_format", sa.String(50)),
        sa.Column("paper_gsm", sa.Integer),
        sa.Column("pen_ink_color", sa.String(100)),
        sa.Column("pen_ink_type", sa.String(100)),
        sa.PrimaryKeyConstraint("catalog_version_id", "sku"),
        sa.ForeignKeyConstraint(["catalog_version_id"], ["catalog_versions.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("category IN ('paper', 'pen')", name="valid_catalog_category"),
        sa.CheckConstraint("pieces_per_sale_unit > 0", name="positive_pack_size"),
        sa.CheckConstraint("price_rub >= 0", name="nonnegative_price"),
    )
    op.create_table(
        "catalog_active",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("catalog_version_id", postgresql.UUID(as_uuid=True)),
        sa.ForeignKeyConstraint(["catalog_version_id"], ["catalog_versions.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("id = 1", name="singleton_active_catalog"),
    )
    op.execute("INSERT INTO catalog_active (id, catalog_version_id) VALUES (1, NULL)")
    op.create_table(
        "idempotency_records",
        sa.Column("namespace", sa.String(60), primary_key=True),
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("result_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["result_version_id"], ["catalog_versions.id"], ondelete="RESTRICT"),
    )


def downgrade() -> None:
    op.drop_table("idempotency_records")
    op.drop_table("catalog_active")
    op.drop_table("catalog_items")
    op.drop_table("catalog_versions")
