"""A22 append-only tenant precision attestations for source supplier rows.

Revision ID: 0021_supplier_precision_reviews
Revises: 0020_quote_review_audit

Only allow the verified working release or a DBA-marked isolated release clone.
No supplier prices are imported or marked fresh by this migration.
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0021_supplier_precision_reviews"
down_revision = "0020_quote_review_audit"
branch_labels = None
depends_on = None
_CLONE = re.compile(r"kpdesk_accounts_http_[0-9a-f]{12}\Z")


def upgrade() -> None:
    connection = op.get_bind()
    database = connection.scalar(sa.text("SELECT current_database()"))
    if database != "kpdesk":
        if not isinstance(database, str) or _CLONE.fullmatch(database) is None:
            raise RuntimeError("A22 requires working KPDesk or a DBA-marked release clone.")
        marker = connection.scalar(sa.text("""
            SELECT shobj_description(oid,'pg_database')
            FROM pg_database WHERE datname=:name
        """), {"name": database})
        if marker != "kpdesk:accounts-http:" + database.rsplit("_", 1)[-1]:
            raise RuntimeError("A22 clone source provenance is invalid.")
    if connection.scalar(sa.text("SELECT current_user")) != "kpdesk":
        raise RuntimeError("A22 requires the restricted schema migration owner.")
    if connection.scalar(sa.text("SELECT version_num FROM alembic_version")) != (
        "0020_quote_review_audit"
    ):
        raise RuntimeError("A22 requires working release revision 0020.")
    if connection.scalar(sa.text(
        "SELECT to_regclass('public.tenant_supplier_precision_reviews')"
    )) is not None:
        raise RuntimeError("A22 supplier precision audit already exists.")

    op.execute("""
        CREATE FUNCTION public.kpdesk_precision_cents(
            amount numeric, mode text
        ) RETURNS numeric LANGUAGE sql IMMUTABLE STRICT
        SET search_path=pg_catalog,pg_temp AS $$
          SELECT CASE mode
          WHEN 'half_up' THEN round(amount, 2)
          WHEN 'half_even' THEN (
            CASE WHEN amount * 100 - trunc(amount * 100) > 0.5
              THEN trunc(amount * 100) + 1
            WHEN amount * 100 - trunc(amount * 100) < 0.5
              THEN trunc(amount * 100)
            WHEN mod(trunc(amount * 100), 2) = 0
              THEN trunc(amount * 100)
            ELSE trunc(amount * 100) + 1 END
          ) / 100
          WHEN 'down' THEN trunc(amount, 2)
          WHEN 'up' THEN ceil(amount * 100) / 100
          ELSE NULL END
        $$;
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_precision_source_numeric(
            p_raw text
        ) RETURNS numeric LANGUAGE sql IMMUTABLE STRICT
        SET search_path=pg_catalog,pg_temp AS $$
          WITH cleaned AS (
            SELECT regexp_replace(
              replace(replace(replace(trim(p_raw),chr(160),''),chr(8239),''),' ',''),
              '(руб[.]?|₽|RUB)$','','i'
            ) AS literal
          )
          SELECT CASE WHEN literal ~ '^[0-9]+[.,][0-9]{3,24}$'
            THEN replace(literal,',','.')::numeric
            ELSE NULL END FROM cleaned
        $$;
    """)
    op.create_table(
        "tenant_supplier_precision_reviews",
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("supplier_id", sa.String(120), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("source_line", sa.Integer(), nullable=False),
        sa.Column("source_row_sha256", sa.String(64), nullable=False),
        sa.Column("source_price_raw", sa.String(128), nullable=False),
        sa.Column("source_price_exact", sa.Numeric(38, 24), nullable=False),
        sa.Column("finding", sa.String(32), nullable=False),
        sa.Column("rounding_mode", sa.String(16)),
        sa.Column("rounded_price_rub", sa.Numeric(15, 2)),
        sa.Column("rationale", sa.String(500), nullable=False),
        sa.Column("reviewer_user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.PrimaryKeyConstraint(
            "organization_id", "supplier_id", "source_sha256",
            "source_line", "reviewer_user_id",
            name="pk_tenant_supplier_precision_review",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_user_id"], ["users.id"], ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "length(trim(supplier_id)) BETWEEN 1 AND 120 "
            "AND length(trim(rationale)) BETWEEN 32 AND 500",
            name="ck_precision_review_bounds",
        ),
        sa.CheckConstraint(
            "source_line > 0 AND "
            "source_sha256 ~ '^[0-9a-f]{64}$' AND "
            "source_row_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_precision_review_source_identity",
        ),
        sa.CheckConstraint(
            "length(source_price_raw) BETWEEN 1 AND 128 "
            "AND source_price_exact>0 AND source_price_exact<1000000000000 "
            "AND public.kpdesk_precision_source_numeric(source_price_raw) "
            "IS NOT NULL "
            "AND source_price_exact="
            "public.kpdesk_precision_source_numeric(source_price_raw)",
            name="ck_precision_review_source_amount",
        ),
        sa.CheckConstraint(
            "finding IN ('approve_rounding','needs_supplier_confirmation',"
            "'reject_source_price')",
            name="ck_precision_review_finding",
        ),
        sa.CheckConstraint(
            "(finding='approve_rounding' AND "
            "rounding_mode IN ('half_up','half_even','down','up') AND "
            "rounded_price_rub>0 AND "
            "rounded_price_rub="
            "public.kpdesk_precision_cents(source_price_exact,rounding_mode)) "
            "OR (finding<>'approve_rounding' AND rounding_mode IS NULL "
            "AND rounded_price_rub IS NULL)",
            name="ck_precision_review_rounded_amount",
        ),
    )
    op.create_index(
        "ix_tenant_precision_source",
        "tenant_supplier_precision_reviews",
        ["organization_id", "supplier_id", "source_sha256", "source_line"],
    )
    op.create_index(
        "ix_tenant_precision_reviewer_time",
        "tenant_supplier_precision_reviews", ["reviewer_user_id", "reviewed_at"],
    )
    op.execute("""
        REVOKE ALL ON TABLE public.tenant_supplier_precision_reviews
        FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime
    """)
    op.execute("""
        ALTER TABLE public.tenant_supplier_precision_reviews
        ENABLE ROW LEVEL SECURITY
    """)
    op.execute("""
        ALTER TABLE public.tenant_supplier_precision_reviews
        FORCE ROW LEVEL SECURITY
    """)
    op.execute("""
        CREATE TRIGGER trg_tenant_supplier_precision_immutable
        BEFORE UPDATE OR DELETE ON public.tenant_supplier_precision_reviews
        FOR EACH ROW EXECUTE FUNCTION public.kpdesk_review_immutable()
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_supplier_precision_attest(
          p_org uuid,p_supplier text,p_source_hash text,p_line integer,
          p_row_hash text,p_price_raw text,p_price_exact numeric,
          p_finding text,p_mode text,p_rounded numeric,p_rationale text
        ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        DECLARE
          actor uuid;
        BEGIN
          IF p_org IS NULL OR p_supplier IS NULL OR
             p_source_hash IS NULL OR p_row_hash IS NULL OR
             p_finding IS NULL OR
             length(trim(p_supplier)) NOT BETWEEN 1 AND 120 OR
             p_source_hash !~ '^[0-9a-f]{64}$' OR
             p_row_hash !~ '^[0-9a-f]{64}$' OR
             p_line IS NULL OR p_line<=0 OR
             p_price_raw IS NULL OR
             length(p_price_raw) NOT BETWEEN 1 AND 128 OR
             p_price_exact IS NULL OR p_price_exact<=0 OR
             p_price_exact>=1000000000000 OR
             public.kpdesk_precision_source_numeric(p_price_raw) IS NULL OR
             p_price_exact IS DISTINCT FROM
               public.kpdesk_precision_source_numeric(p_price_raw) OR
             p_finding NOT IN (
               'approve_rounding','needs_supplier_confirmation','reject_source_price'
             ) OR p_rationale IS NULL OR
             length(trim(p_rationale)) NOT BETWEEN 32 AND 500
          THEN RETURN FALSE; END IF;
          IF p_finding='approve_rounding' THEN
            IF p_mode NOT IN ('half_up','half_even','down','up') OR
               p_mode IS NULL OR p_rounded IS NULL OR
               p_rounded<=0 OR
               p_rounded<>public.kpdesk_precision_cents(p_price_exact,p_mode)
            THEN RETURN FALSE; END IF;
          ELSIF p_mode IS NOT NULL OR p_rounded IS NOT NULL THEN
            RETURN FALSE;
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
          INSERT INTO public.tenant_supplier_precision_reviews(
             organization_id,supplier_id,source_sha256,source_line,
             source_row_sha256,source_price_raw,source_price_exact,
             finding,rounding_mode,rounded_price_rub,rationale,reviewer_user_id
          ) VALUES(
             p_org,p_supplier,p_source_hash,p_line,p_row_hash,
             p_price_raw,p_price_exact,p_finding,p_mode,p_rounded,p_rationale,actor
          ) ON CONFLICT DO NOTHING;
          RETURN EXISTS(
              SELECT 1 FROM public.tenant_supplier_precision_reviews r
              WHERE r.organization_id=p_org AND r.supplier_id=p_supplier
                AND r.source_sha256=p_source_hash AND r.source_line=p_line
                AND r.reviewer_user_id=actor AND r.source_row_sha256=p_row_hash
                AND r.source_price_raw=p_price_raw
                AND r.source_price_exact=p_price_exact AND r.finding=p_finding
                AND r.rounding_mode IS NOT DISTINCT FROM p_mode
                AND r.rounded_price_rub IS NOT DISTINCT FROM p_rounded
                AND r.rationale=p_rationale
          );
        END $$;
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_supplier_precision_get(
          p_org uuid,p_supplier text,p_source_hash text
        ) RETURNS TABLE(
          source_line integer,source_row_sha256 text,source_price_raw text,
          finding text,rounding_mode text,rounded_price_rub numeric,
          rationale text,reviewer_user_id uuid,reviewed_at timestamptz
        ) LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        SELECT r.source_line,r.source_row_sha256::text,
               r.source_price_raw::text,r.finding::text,
               r.rounding_mode::text,r.rounded_price_rub,
               r.rationale::text,r.reviewer_user_id,r.reviewed_at
        FROM public.tenant_supplier_precision_reviews r
        WHERE r.organization_id=p_org AND r.supplier_id=p_supplier
          AND r.source_sha256=p_source_hash
          AND public.kpdesk_session_tenant_allowed(p_org,'read')
        ORDER BY r.source_line,r.reviewed_at,r.reviewer_user_id
        $$;
    """)
    for signature in (
        "kpdesk_precision_cents(numeric,text)",
        "kpdesk_precision_source_numeric(text)",
        "kpdesk_supplier_precision_attest(uuid,text,text,integer,text,text,numeric,text,text,numeric,text)",
        "kpdesk_supplier_precision_get(uuid,text,text)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{signature} TO kpdesk_tenant_runtime")


def downgrade() -> None:
    raise RuntimeError("A22 immutable supplier price review requires retention and backup.")
