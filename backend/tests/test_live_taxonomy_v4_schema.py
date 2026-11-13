"""Read-only acceptance for local KPDesk's released 40-category PostgreSQL.

Migrations must evolve together with Python taxonomy and tenant SQL security.
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from kpdesk.matching.profiles import CATEGORY_ATTRIBUTES


def test_local_release_schema_0031_matches_all_40_categories() -> None:
    settings = dotenv_values(Path(__file__).resolve().parents[2] / ".env")
    password = settings.get("KP_DESK_DB_PASSWORD")
    assert isinstance(password, str) and len(password) >= 32
    engine = create_engine(
        URL.create(
            "postgresql+psycopg", username="kpdesk", password=password,
            host="127.0.0.1", port=int(settings.get("KP_DESK_DB_PORT") or 55432),
            database="kpdesk",
        ),
        connect_args={"options": "-c default_transaction_read_only=on"},
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            db, role, revision = connection.execute(text("""
                SELECT current_database(), current_user,
                       (SELECT version_num FROM public.alembic_version)
            """)).one()
            assert (db, role, revision) == (
                "kpdesk", "kpdesk", "0031_catalog_taxonomy_v4",
            )
            check = connection.execute(text("""
                SELECT pg_get_constraintdef(c.oid), c.convalidated,
                       t.relrowsecurity, t.relforcerowsecurity
                FROM pg_constraint c
                JOIN pg_class t ON t.oid=c.conrelid
                WHERE c.conrelid='public.catalog_items'::regclass
                  AND c.conname='valid_catalog_category'
                  AND c.contype='c'
            """)).one()
            release = importlib.import_module(
                "migrations.versions.0031_catalog_taxonomy_v4"
            )
            categories = set(re.findall(r"'([a-z_]+)'", check[0]))
            assert check[1] is True
            assert categories == set(release.V4_CATEGORIES)
            assert categories == set(CATEGORY_ATTRIBUTES)
            assert len(categories) == 40
            assert categories - set(release.V3_CATEGORIES) == set(
                release.NEW_CATEGORIES
            )
            grants = connection.execute(text("""
                SELECT has_table_privilege(
                           'kpdesk_tenant_runtime','public.catalog_items','SELECT'),
                       has_table_privilege(
                           'kpdesk_tenant_runtime','public.catalog_items','INSERT'),
                       has_table_privilege(
                           'kpdesk_app','public.catalog_items','INSERT')
            """)).one()
            assert len(grants) == 3
            assert all(type(value) is bool for value in grants)
            assert all(type(value) is bool for value in (check[2], check[3]))
    finally:
        engine.dispose()
