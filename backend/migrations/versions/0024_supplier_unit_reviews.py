"""A31 tenant-source-bound immutable review of supplier price unit and pack size.

Revision ID: 0024_supplier_unit_reviews
Revises: 0023_supplier_mapping_history

Does not approve products, publish prices, or produce ML labels.
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0024_supplier_unit_reviews"
down_revision = "0023_supplier_mapping_history"
branch_labels = None
depends_on = None

_CLONE = re.compile(r"kpdesk_accounts_http_[0-9a-f]{12}\Z")


def upgrade() -> None:
    connection = op.get_bind()
    database = connection.scalar(sa.text("SELECT current_database()"))
    if database != "kpdesk":
        if not isinstance(database, str) or _CLONE.fullmatch(database) is None:
            raise RuntimeError("A31 only permits KPDesk or an approved release clone.")
        marker = connection.scalar(sa.text("""
            SELECT shobj_description(oid,'pg_database')
            FROM pg_database WHERE datname=:name
        """), {"name": database})
        if marker != "kpdesk:accounts-http:" + database.rsplit("_", 1)[-1]:
            raise RuntimeError("A31 release clone provenance is invalid.")
    if connection.scalar(sa.text("SELECT current_user")) != "kpdesk":
        raise RuntimeError("A31 requires the restricted schema migration owner.")
    if connection.scalar(sa.text("SELECT version_num FROM alembic_version")) != (
        "0023_supplier_mapping_history"
    ):
        raise RuntimeError("A31 requires release revision 0023.")
    if connection.scalar(sa.text(
        "SELECT to_regclass('public.tenant_supplier_unit_reviews')"
    )) is not None:
        raise RuntimeError("A31 supplier unit audit already exists.")

    op.create_table(
        "tenant_supplier_unit_reviews",
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("supplier_id", sa.String(120), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("source_line", sa.Integer(), nullable=False),
        sa.Column("source_row_sha256", sa.String(64), nullable=False),
        sa.Column("source_sale_unit", sa.Text()),
        sa.Column("source_pack_size", sa.Integer()),
        sa.Column("finding", sa.String(40), nullable=False),
        sa.Column("approved_sale_unit", sa.String(16)),
        sa.Column("approved_pack_size", sa.Integer()),
        sa.Column("rationale", sa.String(500), nullable=False),
        sa.Column("reviewer_user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.PrimaryKeyConstraint(
            "organization_id", "supplier_id", "source_sha256",
            "source_line", "reviewer_user_id",
            name="pk_tenant_supplier_unit_review",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_user_id"], ["users.id"], ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "supplier_id ~ '^[A-Za-z0-9_.-]{1,120}$' "
            "AND source_sha256 ~ '^[0-9a-f]{64}$' "
            "AND source_row_sha256 ~ '^[0-9a-f]{64}$' AND source_line>0",
            name="ck_supplier_unit_review_identity",
        ),
        sa.CheckConstraint(
            "(source_sale_unit IS NULL OR length(source_sale_unit)<=2000) "
            "AND (source_pack_size IS NULL OR source_pack_size BETWEEN 1 AND 100000)",
            name="ck_supplier_unit_review_source_bounds",
        ),
        sa.CheckConstraint(
            "length(trim(rationale)) BETWEEN 32 AND 500",
            name="ck_supplier_unit_review_evidence",
        ),
        sa.CheckConstraint(
            "(finding='confirm_price_unit' "
            "AND approved_sale_unit IN ('штука','пачка','коробка') "
            "AND approved_pack_size BETWEEN 1 AND 100000) "
            "OR (finding IN ('needs_supplier_confirmation','reject_source_unit') "
            "AND approved_sale_unit IS NULL AND approved_pack_size IS NULL)",
            name="ck_supplier_unit_review_decision",
        ),
    )
    op.create_index(
        "ix_supplier_unit_review_source",
        "tenant_supplier_unit_reviews",
        ["organization_id", "supplier_id", "source_sha256", "source_line"],
    )
    op.execute("""
        REVOKE ALL ON TABLE public.tenant_supplier_unit_reviews
        FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime
    """)
    op.execute("""
        ALTER TABLE public.tenant_supplier_unit_reviews
        ENABLE ROW LEVEL SECURITY
    """)
    op.execute("""
        ALTER TABLE public.tenant_supplier_unit_reviews
        FORCE ROW LEVEL SECURITY
    """)
    op.execute("""
        CREATE TRIGGER trg_tenant_supplier_unit_immutable
        BEFORE UPDATE OR DELETE ON public.tenant_supplier_unit_reviews
        FOR EACH ROW EXECUTE FUNCTION public.kpdesk_review_immutable()
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_supplier_unit_attest(
          p_org uuid,p_supplier text,p_source_hash text,p_line integer,
          p_row_hash text,p_source_unit text,p_source_pack integer,
          p_finding text,p_approved_unit text,p_approved_pack integer,
          p_rationale text
        ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        DECLARE actor uuid;
        BEGIN
          IF p_org IS NULL OR p_supplier IS NULL OR
             p_supplier !~ '^[A-Za-z0-9_.-]{1,120}$' OR
             p_source_hash IS NULL OR p_source_hash !~ '^[0-9a-f]{64}$' OR
             p_row_hash IS NULL OR p_row_hash !~ '^[0-9a-f]{64}$' OR
             p_line IS NULL OR p_line<=0 OR
             (p_source_unit IS NOT NULL AND length(p_source_unit)>2000) OR
             (p_source_pack IS NOT NULL AND
              (p_source_pack<1 OR p_source_pack>100000)) OR
             p_rationale IS NULL OR
             length(trim(p_rationale)) NOT BETWEEN 32 AND 500 OR
             p_finding IS NULL
          THEN RETURN FALSE; END IF;
          IF p_finding='confirm_price_unit' THEN
             IF p_approved_unit IS NULL OR
                p_approved_unit NOT IN ('штука','пачка','коробка') OR
                p_approved_pack IS NULL OR
                p_approved_pack NOT BETWEEN 1 AND 100000
             THEN RETURN FALSE; END IF;
          ELSIF p_finding IN ('needs_supplier_confirmation','reject_source_unit') THEN
             IF p_approved_unit IS NOT NULL OR p_approved_pack IS NOT NULL
             THEN RETURN FALSE; END IF;
          ELSE RETURN FALSE;
          END IF;
          IF current_setting('kpdesk.session_sha256',true)
             !~ '^[0-9a-f]{64}$' OR
             NOT public.kpdesk_session_tenant_allowed(p_org,'write')
          THEN RETURN FALSE; END IF;
          SELECT s.user_id INTO actor
          FROM public.auth_sessions s
          JOIN public.users u ON u.id=s.user_id
          JOIN public.organization_memberships m
            ON m.organization_id=p_org AND m.user_id=s.user_id
          WHERE s.token_sha256=current_setting('kpdesk.session_sha256',true)
            AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp()
            AND u.status='active' AND u.email_verified_at IS NOT NULL
            AND m.status='active' AND m.role IN ('owner','editor');
          IF actor IS NULL THEN RETURN FALSE; END IF;
          INSERT INTO public.tenant_supplier_unit_reviews(
             organization_id,supplier_id,source_sha256,source_line,
             source_row_sha256,source_sale_unit,source_pack_size,
             finding,approved_sale_unit,approved_pack_size,rationale,reviewer_user_id
          ) VALUES (
             p_org,p_supplier,p_source_hash,p_line,p_row_hash,
             p_source_unit,p_source_pack,p_finding,p_approved_unit,
             p_approved_pack,p_rationale,actor
          ) ON CONFLICT DO NOTHING;
          RETURN EXISTS(
            SELECT 1 FROM public.tenant_supplier_unit_reviews r
            WHERE r.organization_id=p_org AND r.supplier_id=p_supplier
              AND r.source_sha256=p_source_hash AND r.source_line=p_line
              AND r.reviewer_user_id=actor AND r.source_row_sha256=p_row_hash
              AND r.source_sale_unit IS NOT DISTINCT FROM p_source_unit
              AND r.source_pack_size IS NOT DISTINCT FROM p_source_pack
              AND r.finding=p_finding
              AND r.approved_sale_unit IS NOT DISTINCT FROM p_approved_unit
              AND r.approved_pack_size IS NOT DISTINCT FROM p_approved_pack
              AND r.rationale=p_rationale
          );
        END $$;
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_supplier_unit_get(
          p_org uuid,p_supplier text,p_source_hash text,
          p_limit integer,p_offset integer
        ) RETURNS TABLE(
          source_line integer,source_row_sha256 text,source_sale_unit text,
          source_pack_size integer,finding text,approved_sale_unit text,
          approved_pack_size integer,rationale text,reviewer_user_id uuid,
          reviewed_at timestamptz
        ) LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
          SELECT r.source_line,r.source_row_sha256::text,r.source_sale_unit,
                 r.source_pack_size,r.finding::text,r.approved_sale_unit::text,
                 r.approved_pack_size,r.rationale::text,r.reviewer_user_id,
                 r.reviewed_at
          FROM public.tenant_supplier_unit_reviews r
          WHERE p_org IS NOT NULL
            AND p_supplier ~ '^[A-Za-z0-9_.-]{1,120}$'
            AND p_source_hash ~ '^[0-9a-f]{64}$'
            AND p_limit BETWEEN 1 AND 100 AND p_offset BETWEEN 0 AND 5000
            AND public.kpdesk_session_tenant_allowed(p_org,'read')
            AND r.organization_id=p_org AND r.supplier_id=p_supplier
            AND r.source_sha256=p_source_hash
          ORDER BY r.source_line,r.reviewed_at,r.reviewer_user_id
          LIMIT p_limit OFFSET p_offset
        $$;
    """)
    for signature in (
        "kpdesk_supplier_unit_attest(uuid,text,text,integer,text,text,integer,text,text,integer,text)",
        "kpdesk_supplier_unit_get(uuid,text,text,integer,integer)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{signature} TO kpdesk_tenant_runtime")


def downgrade() -> None:
    raise RuntimeError("A31 immutable supplier unit reviews require retention and backup.")
