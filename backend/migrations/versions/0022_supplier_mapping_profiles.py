"""A25 immutable tenant supplier mapping profiles.

Revision ID: 0022_supplier_mapping_profiles
Revises: 0021_supplier_precision_reviews

Source prices are NOT stored or approved through this revision.
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0022_supplier_mapping_profiles"
down_revision = "0021_supplier_precision_reviews"
branch_labels = None
depends_on = None
_CLONE = re.compile(r"kpdesk_accounts_http_[0-9a-f]{12}\Z")


def upgrade() -> None:
    conn = op.get_bind()
    database = conn.scalar(sa.text("SELECT current_database()"))
    if database != "kpdesk":
        if not isinstance(database, str) or _CLONE.fullmatch(database) is None:
            raise RuntimeError("A25 requires working KPDesk or a verified release clone.")
        marker = conn.scalar(sa.text("""
            SELECT shobj_description(oid,'pg_database')
            FROM pg_database WHERE datname=:name
        """), {"name": database})
        if marker != "kpdesk:accounts-http:" + database.rsplit("_", 1)[-1]:
            raise RuntimeError("A25 release clone provenance invalid.")
    if conn.scalar(sa.text("SELECT current_user")) != "kpdesk":
        raise RuntimeError("A25 requires restricted schema migration owner.")
    if conn.scalar(sa.text("SELECT version_num FROM alembic_version")) != (
        "0021_supplier_precision_reviews"
    ):
        raise RuntimeError("A25 requires working release revision 0021.")
    if conn.scalar(sa.text("SELECT to_regclass('public.tenant_supplier_mapping_profiles')")):
        raise RuntimeError("A25 mapping profile schema exists already.")

    op.create_table(
        "tenant_supplier_mapping_profiles",
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("supplier_id", sa.String(120), nullable=False),
        sa.Column("profile_sha256", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("profile", JSONB(), nullable=False),
        sa.Column("reviewer_user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.PrimaryKeyConstraint(
            "organization_id", "supplier_id", "profile_sha256",
            name="pk_tenant_supplier_mapping_profile",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_user_id"], ["users.id"], ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "supplier_id ~ '^[A-Za-z0-9_.-]{1,120}$' AND "
            "profile_sha256 ~ '^[0-9a-f]{64}$' AND fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_supplier_mapping_profile_identity",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(profile)='object' AND "
            "profile->>'profile_sha256'=profile_sha256 AND "
            "profile->>'fingerprint'=fingerprint AND "
            "profile->>'organization_id'=organization_id::text AND "
            "profile->>'supplier_id'=supplier_id AND "
            "profile->>'approved_by'=reviewer_user_id::text AND "
            "profile->>'approved_source_sha256' ~ '^[0-9a-f]{64}$' AND "
            "profile->>'source_format' IN ('csv','xls','xlsx','json') AND "
            "profile->>'price_basis' IN "
            "('standard','retail','wholesale','rrp','vat_included') AND "
            "jsonb_typeof(profile->'columns')='array'",
            name="ck_supplier_mapping_profile_evidence",
        ),
    )
    op.create_index(
        "ix_supplier_mapping_profile_lookup", "tenant_supplier_mapping_profiles",
        ["organization_id", "supplier_id", "fingerprint", "reviewed_at"],
    )
    op.execute("""
        REVOKE ALL ON TABLE public.tenant_supplier_mapping_profiles
          FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime
    """)
    op.execute("""
        ALTER TABLE public.tenant_supplier_mapping_profiles ENABLE ROW LEVEL SECURITY
    """)
    op.execute("""
        ALTER TABLE public.tenant_supplier_mapping_profiles FORCE ROW LEVEL SECURITY
    """)
    op.execute("""
        CREATE TRIGGER trg_tenant_supplier_mapping_immutable
        BEFORE UPDATE OR DELETE ON public.tenant_supplier_mapping_profiles
        FOR EACH ROW EXECUTE FUNCTION public.kpdesk_review_immutable()
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_supplier_mapping_save(
          p_org uuid,p_supplier text,p_profile jsonb
        ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        DECLARE actor uuid;
        BEGIN
          IF p_org IS NULL OR p_supplier IS NULL OR
             p_supplier !~ '^[A-Za-z0-9_.-]{1,120}$'
             OR p_profile IS NULL OR jsonb_typeof(p_profile)<>'object'
             OR length(p_profile::text)>8192
             OR p_profile->>'organization_id' IS DISTINCT FROM p_org::text
             OR p_profile->>'supplier_id' IS DISTINCT FROM p_supplier
             OR p_profile->>'fingerprint' IS NULL
             OR p_profile->>'fingerprint' !~ '^[0-9a-f]{64}$'
             OR p_profile->>'profile_sha256' IS NULL
             OR p_profile->>'profile_sha256' !~ '^[0-9a-f]{64}$'
             OR p_profile->>'approved_source_sha256' IS NULL
             OR p_profile->>'approved_source_sha256' !~ '^[0-9a-f]{64}$'
             OR p_profile->>'price_basis' NOT IN
               ('standard','retail','wholesale','rrp','vat_included')
             OR p_profile->>'source_format' NOT IN ('csv','xls','xlsx','json')
             OR jsonb_typeof(p_profile->'columns') IS DISTINCT FROM 'array'
             OR length(trim(coalesce(p_profile->>'review_evidence',''))) NOT BETWEEN 8 AND 500
             OR current_setting('kpdesk.session_sha256',true) !~ '^[0-9a-f]{64}$'
             OR NOT public.kpdesk_session_tenant_allowed(p_org,'write')
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
          IF actor IS NULL OR p_profile->>'approved_by' IS DISTINCT FROM actor::text
          THEN RETURN FALSE; END IF;
          INSERT INTO public.tenant_supplier_mapping_profiles(
            organization_id,supplier_id,profile_sha256,fingerprint,profile,reviewer_user_id
          ) VALUES (
            p_org,p_supplier,p_profile->>'profile_sha256',
            p_profile->>'fingerprint',p_profile,actor
          ) ON CONFLICT DO NOTHING;
          RETURN EXISTS(
            SELECT 1 FROM public.tenant_supplier_mapping_profiles r
            WHERE r.organization_id=p_org AND r.supplier_id=p_supplier
              AND r.profile_sha256=p_profile->>'profile_sha256'
              AND r.profile=p_profile AND r.reviewer_user_id=actor
          );
        END $$;
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_supplier_mapping_latest(
          p_org uuid,p_supplier text,p_fingerprint text
        ) RETURNS TABLE(profile jsonb,reviewed_at timestamptz)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
          SELECT r.profile,r.reviewed_at
          FROM public.tenant_supplier_mapping_profiles r
          WHERE r.organization_id=p_org AND r.supplier_id=p_supplier
            AND r.fingerprint=p_fingerprint
            AND public.kpdesk_session_tenant_allowed(p_org,'read')
          ORDER BY r.reviewed_at DESC,r.profile_sha256 DESC
          LIMIT 1
        $$;
    """)
    for signature in (
        "kpdesk_supplier_mapping_save(uuid,text,jsonb)",
        "kpdesk_supplier_mapping_latest(uuid,text,text)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{signature} TO kpdesk_tenant_runtime")


def downgrade() -> None:
    raise RuntimeError("A25 immutable supplier mapping profiles require retention and backup.")
