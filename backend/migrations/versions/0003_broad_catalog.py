"""Widen catalog category and expand versioned taxonomy without deleting old versions.

Revision ID: 0003_broad_catalog
Revises: 0002_catalog_profiles
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_broad_catalog"
down_revision = "0002_catalog_profiles"
branch_labels = None
depends_on = None

V3 = "category IN ('paper', 'pen', 'pencil', 'mechanical_pencil', 'marker', 'highlighter', 'eraser', 'sharpener', 'notebook', 'notepad', 'document_folder', 'binder', 'plastic_sleeve', 'envelope', 'label', 'adhesive_tape', 'glue', 'correction_fluid', 'staple', 'scissors', 'ruler', 'folder', 'stapler', 'paperclip', 'keyboard', 'mouse', 'usb_drive', 'toner_cartridge', 'ink_cartridge', 'printer', 'mfp', 'copier', 'photo_paper', 'stationery_other')"
V2 = "category IN ('paper', 'pen', 'folder', 'stapler', 'paperclip', 'keyboard', 'mouse', 'usb_drive')"


def upgrade() -> None:
    op.alter_column(
        "catalog_items", "category",
        existing_type=sa.String(length=10), type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.drop_constraint("valid_catalog_category", "catalog_items", type_="check")
    op.create_check_constraint("valid_catalog_category", "catalog_items", V3)


def downgrade() -> None:
    rows = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM catalog_items WHERE NOT (" + V2 + ")"
    )).scalar_one()
    if rows:
        raise RuntimeError("Cannot downgrade: v3 category rows exist")
    op.drop_constraint("valid_catalog_category", "catalog_items", type_="check")
    op.create_check_constraint("valid_catalog_category", "catalog_items", V2)
    op.alter_column(
        "catalog_items", "category", existing_type=sa.String(length=64),
        type_=sa.String(length=10), existing_nullable=False,
    )
