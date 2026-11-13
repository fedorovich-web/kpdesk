"""A44: session-bound, read-only access to immutable quote SKU attestations.

Revision ID: 0029_manager_decisions_read
Revises: 0028_quote_manager_decisions
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op

revision = "0029_manager_decisions_read"
down_revision = "0028_quote_manager_decisions"
branch_labels = None
depends_on = None

_CLONE = re.compile(r"kpdesk_accounts_http_[0-9a-f]{12}\Z")
_SIGNATURE = "kpdesk_quote_manager_decisions_get(uuid,uuid,integer)"


def upgrade() -> None:
    connection = op.get_bind()
    database = connection.scalar(sa.text("SELECT current_database()"))
    if database != "kpdesk":
        if not isinstance(database, str) or _CLONE.fullmatch(database) is None:
            raise RuntimeError("A44 requires KPDesk or a marked disposable clone.")
        marker = connection.scalar(sa.text("""
            SELECT shobj_description(oid,'pg_database')
            FROM pg_database WHERE datname=:database
        """), {"database": database})
        if marker != "kpdesk:accounts-http:" + database.rsplit("_", 1)[-1]:
            raise RuntimeError("A44 disposable clone provenance mismatch.")
    if connection.scalar(sa.text("SELECT current_user")) != "kpdesk":
        raise RuntimeError("A44 requires verified PostgreSQL schema owner.")
    if connection.scalar(sa.text("SELECT version_num FROM alembic_version")) != (
        "0028_quote_manager_decisions"
    ):
        raise RuntimeError("A44 requires the exact revision 0028.")
    if connection.scalar(sa.text(
        "SELECT to_regclass('public.tenant_quote_manager_decisions')"
    )) is None:
        raise RuntimeError("A44 requires the immutable manager decision table.")
    if connection.scalar(sa.text(
        "SELECT to_regprocedure('public." + _SIGNATURE + "')"
    )) is not None:
        raise RuntimeError("A44 manager decision read function already exists.")

    op.execute("""
        CREATE FUNCTION public.kpdesk_quote_manager_decisions_get(
          p_org uuid, p_quote uuid, p_revision integer
        ) RETURNS TABLE(
          line_number integer, sku text, catalog_version_id uuid,
          approved_by_user_id uuid, recorded_at timestamptz
        ) LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
          SELECT d.line_number, d.sku::text, d.catalog_version_id,
                 d.approved_by_user_id, d.recorded_at
          FROM public.tenant_quote_manager_decisions d
          WHERE p_org IS NOT NULL AND p_quote IS NOT NULL
            AND p_revision BETWEEN 1 AND 2147483647
            AND public.kpdesk_session_tenant_allowed(p_org,'read')
            AND d.organization_id=p_org AND d.quote_id=p_quote
            AND d.revision=p_revision
          ORDER BY d.line_number
        $$;
    """)
    op.execute(f"REVOKE ALL ON FUNCTION public.{_SIGNATURE} FROM PUBLIC")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.{_SIGNATURE} "
        "TO kpdesk_tenant_runtime"
    )


def downgrade() -> None:
    raise RuntimeError(
        "Immutable manager-decision audit access must be removed by reviewed migration."
    )
