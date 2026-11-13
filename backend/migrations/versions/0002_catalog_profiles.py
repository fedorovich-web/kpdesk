"""Expanded typed catalog profiles, preserving existing catalog versions.

Revision ID: 0002_catalog_profiles
Revises: 0001_catalog
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_catalog_profiles"
down_revision = "0001_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "catalog_items",
        sa.Column(
            "attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.drop_constraint("valid_catalog_category", "catalog_items", type_="check")
    op.create_check_constraint(
        "valid_catalog_category",
        "catalog_items",
        "category IN ('paper', 'pen', 'folder', 'stapler', 'paperclip', "
        "'keyboard', 'mouse', 'usb_drive')",
    )


def downgrade() -> None:
    rows = op.get_bind().execute(
        sa.text(
            "SELECT count(*) FROM catalog_items "
            "WHERE category NOT IN ('paper', 'pen')"
        )
    ).scalar_one()
    if rows:
        raise RuntimeError(
            "Cannot downgrade: new-category catalog versions would be lost."
        )
    op.drop_constraint("valid_catalog_category", "catalog_items", type_="check")
    op.create_check_constraint(
        "valid_catalog_category", "catalog_items",
        "category IN ('paper', 'pen')",
    )
    op.drop_column("catalog_items", "attributes")
