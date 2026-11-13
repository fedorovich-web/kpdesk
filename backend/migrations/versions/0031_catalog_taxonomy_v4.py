"""Synchronize released catalog CHECK constraint with taxonomy v4.

Revision ID: 0031_catalog_taxonomy_v4
Revises: 0030_tenant_llm_cache

Local schema updates require KP_DESK_APPROVE_LOCAL_0031=1, the exact
registered owner/database and an empty live application database.
Disposable clone tests are independently provenance-checked.

No data rewrite, role/grant/index changes, category rewriting, or autostamp.
"""
from __future__ import annotations

import os
import re

import sqlalchemy as sa
from alembic import op

revision = "0031_catalog_taxonomy_v4"
down_revision = "0030_tenant_llm_cache"
branch_labels = None
depends_on = None

# Frozen schema vocabulary. DO NOT import mutable application taxonomy inside
# an Alembic migration: old migrations must remain deterministic over time.
V3_CATEGORIES: tuple[str, ...] = (
    "paper", "pen", "pencil", "mechanical_pencil", "marker",
    "highlighter", "eraser", "sharpener", "notebook", "notepad",
    "document_folder", "binder", "plastic_sleeve", "envelope",
    "label", "adhesive_tape", "glue", "correction_fluid", "staple",
    "scissors", "ruler", "folder", "stapler", "paperclip",
    "keyboard", "mouse", "usb_drive", "toner_cartridge",
    "ink_cartridge", "printer", "mfp", "copier", "photo_paper",
    "stationery_other",
)
NEW_CATEGORIES: tuple[str, ...] = (
    "marker_refill", "mouse_pad", "notebook_cover",
    "pen_refill", "staple_remover", "usb_adapter",
)
V4_CATEGORIES: tuple[str, ...] = tuple(sorted((*V3_CATEGORIES, *NEW_CATEGORIES)))
_CLONE = re.compile(r"kpdesk_accounts_http_[0-9a-f]{12}\Z")


def _guard_target(*, expected_revision: str) -> sa.Connection:
    connection = op.get_bind()
    database, role, version = connection.execute(sa.text("""
        SELECT current_database(), current_user,
               (SELECT version_num FROM public.alembic_version)
    """)).one()
    if role != "kpdesk" or version != expected_revision:
        raise RuntimeError("A97 requires exact schema owner and expected Alembic version.")
    if database == "kpdesk":
        if os.environ.get("KP_DESK_APPROVE_LOCAL_0031") != "1":
            raise RuntimeError("A97 live migration requires explicit local approval flag.")
        counts = connection.execute(sa.text("""
            SELECT (SELECT count(*) FROM public.organizations),
                   (SELECT count(*) FROM public.users),
                   (SELECT count(*) FROM public.catalog_items),
                   (SELECT count(*) FROM public.quote_documents),
                   (SELECT count(*) FROM public.tenant_llm_attempts)
        """)).one()
        if counts != (0, 0, 0, 0, 0):
            raise RuntimeError("A97 live migration requires empty application tables.")
    elif isinstance(database, str) and _CLONE.fullmatch(database):
        marker = connection.scalar(sa.text("""
            SELECT shobj_description(oid,'pg_database')
            FROM pg_database WHERE datname=:database
        """), {"database": database})
        if marker != "kpdesk:accounts-http:" + database.rsplit("_", 1)[-1]:
            raise RuntimeError("A97 disposable clone provenance mismatch.")
    else:
        raise RuntimeError("A97 refuses unrecognized database.")
    connection.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    return connection


def _existing_categories(connection: sa.Connection) -> frozenset[str]:
    definition = connection.scalar(sa.text("""
        SELECT pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE conrelid='public.catalog_items'::regclass
          AND conname='valid_catalog_category' AND contype='c'
    """))
    if definition is None:
        raise RuntimeError("A97 catalog category CHECK is absent.")
    if not isinstance(definition, str):
        raise TypeError("A97 catalog category CHECK has an invalid type.")
    return frozenset(re.findall(r"'([a-z_]+)'", definition))


def _predicate(categories: tuple[str, ...]) -> str:
    if (
        not categories
        or len(set(categories)) != len(categories)
        or any(re.fullmatch(r"[a-z_]+", category) is None for category in categories)
    ):
        raise RuntimeError("A97 frozen category identifiers are invalid.")
    return "category IN (" + ",".join("'" + category + "'" for category in categories) + ")"


def upgrade() -> None:
    connection = _guard_target(expected_revision=down_revision)
    if _existing_categories(connection) != frozenset(V3_CATEGORIES):
        raise RuntimeError("A97 upgrade requires the exact 34-category released CHECK.")
    op.drop_constraint("valid_catalog_category", "catalog_items", schema="public", type_="check")
    op.create_check_constraint(
        "valid_catalog_category", "catalog_items",
        _predicate(V4_CATEGORIES), schema="public",
    )
    if _existing_categories(connection) != frozenset(V4_CATEGORIES):
        raise RuntimeError("A97 expanded CHECK could not be verified.")


def downgrade() -> None:
    connection = _guard_target(expected_revision=revision)
    if _existing_categories(connection) != frozenset(V4_CATEGORIES):
        raise RuntimeError("A97 downgrade requires exactly 40 permitted categories.")
    # Old and new catalog versions coexist. Never silently remove, relabel,
    # or invalidate historical tenant SKUs in order to satisfy the old CHECK.
    foreign = connection.scalar(sa.text("""
        SELECT count(*) FROM public.catalog_items
        WHERE category = ANY(CAST(:new_categories AS text[]))
    """), {"new_categories": list(NEW_CATEGORIES)})
    if foreign != 0:
        raise RuntimeError(
            "A97 downgrade refused: catalog history contains post-v3 categories."
        )
    op.drop_constraint("valid_catalog_category", "catalog_items", schema="public", type_="check")
    op.create_check_constraint(
        "valid_catalog_category", "catalog_items",
        _predicate(V3_CATEGORIES), schema="public",
    )
    if _existing_categories(connection) != frozenset(V3_CATEGORIES):
        raise RuntimeError("A97 restored CHECK could not be verified.")
