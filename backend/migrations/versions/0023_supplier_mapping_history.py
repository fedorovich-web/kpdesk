"""A26 bounded, session-bound read access to immutable supplier mapping history.

Revision ID: 0023_supplier_mapping_history
Revises: 0022_supplier_mapping_profiles
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op

revision = "0023_supplier_mapping_history"
down_revision = "0022_supplier_mapping_profiles"
branch_labels = None
depends_on = None

_CLONE = re.compile(r"kpdesk_accounts_http_[0-9a-f]{12}\Z")


def upgrade() -> None:
    connection = op.get_bind()
    database = connection.scalar(sa.text("SELECT current_database()"))
    if database != "kpdesk":
        if not isinstance(database, str) or _CLONE.fullmatch(database) is None:
            raise RuntimeError("A26 requires KPDesk or a verified release clone.")
        marker = connection.scalar(sa.text("""
            SELECT shobj_description(oid,'pg_database')
            FROM pg_database WHERE datname=:name
        """), {"name": database})
        if marker != "kpdesk:accounts-http:" + database.rsplit("_", 1)[-1]:
            raise RuntimeError("A26 release clone provenance invalid.")
    if connection.scalar(sa.text("SELECT current_user")) != "kpdesk":
        raise RuntimeError("A26 requires restricted schema migration owner.")
    if connection.scalar(sa.text("SELECT version_num FROM alembic_version")) != (
        "0022_supplier_mapping_profiles"
    ):
        raise RuntimeError("A26 requires release revision 0022.")
    if connection.scalar(sa.text("""
        SELECT to_regprocedure(
          'public.kpdesk_supplier_mapping_history(uuid,text,integer,integer)'
        ) IS NOT NULL
    """)):
        raise RuntimeError("A26 history function already exists.")

    op.execute("""
        CREATE FUNCTION public.kpdesk_supplier_mapping_history(
          p_org uuid,p_supplier text,p_limit integer,p_offset integer
        ) RETURNS TABLE(profile jsonb,reviewed_at timestamptz)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
          SELECT r.profile,r.reviewed_at
          FROM public.tenant_supplier_mapping_profiles r
          WHERE p_org IS NOT NULL
            AND p_supplier ~ '^[A-Za-z0-9_.-]{1,120}$'
            AND p_limit BETWEEN 1 AND 50
            AND p_offset BETWEEN 0 AND 500
            AND public.kpdesk_session_tenant_allowed(p_org,'read')
            AND r.organization_id=p_org AND r.supplier_id=p_supplier
          ORDER BY r.reviewed_at DESC,r.profile_sha256 DESC
          LIMIT p_limit OFFSET p_offset
        $$;
    """)
    # A25 returned the most recent profile even when two reviewers saved
    # conflicting column/price decisions for the same source structure.
    # Exact-schema reuse must fail closed until an operator resolves that drift.
    # Approval identity, source bytes, and rationale are audit evidence rather
    # than part of the layout decision, so those may legitimately differ.
    op.execute("""
        CREATE OR REPLACE FUNCTION public.kpdesk_supplier_mapping_latest(
          p_org uuid,p_supplier text,p_fingerprint text
        ) RETURNS TABLE(profile jsonb,reviewed_at timestamptz)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
          SELECT r.profile,r.reviewed_at
          FROM public.tenant_supplier_mapping_profiles r
          WHERE r.organization_id=p_org AND r.supplier_id=p_supplier
            AND r.fingerprint=p_fingerprint
            AND public.kpdesk_session_tenant_allowed(p_org,'read')
            AND NOT EXISTS (
              SELECT 1 FROM public.tenant_supplier_mapping_profiles other
              WHERE other.organization_id=r.organization_id
                AND other.supplier_id=r.supplier_id
                AND other.fingerprint=r.fingerprint
                AND (other.profile - 'organization_id' - 'supplier_id'
                     - 'fingerprint' - 'profile_sha256' - 'approved_by'
                     - 'approved_source_sha256' - 'review_evidence')
                  IS DISTINCT FROM
                    (r.profile - 'organization_id' - 'supplier_id'
                     - 'fingerprint' - 'profile_sha256' - 'approved_by'
                     - 'approved_source_sha256' - 'review_evidence')
            )
          ORDER BY r.reviewed_at DESC,r.profile_sha256 DESC
          LIMIT 1
        $$;
    """)
    op.execute("""
        REVOKE ALL ON FUNCTION
          public.kpdesk_supplier_mapping_history(uuid,text,integer,integer)
        FROM PUBLIC
    """)
    op.execute("""
        GRANT EXECUTE ON FUNCTION
          public.kpdesk_supplier_mapping_history(uuid,text,integer,integer)
        TO kpdesk_tenant_runtime
    """)


def downgrade() -> None:
    raise RuntimeError("A26 supplier approval audit read contract is append-only.")
