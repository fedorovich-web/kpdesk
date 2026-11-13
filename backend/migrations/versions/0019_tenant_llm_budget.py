"""A20 live bounded LLM inference reservation with atomic SQL quotas.

Revision ID: 0019_tenant_llm_budget
Revises: 0018_email_team_onboarding

This is a CALL/character/concurrency guardrail, NOT a financial billing
limit: actual Groq rates and billed token counts are not assumed.
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0019_tenant_llm_budget"
down_revision = "0018_email_team_onboarding"
branch_labels = None
depends_on = None

_CLONE = re.compile(r"kpdesk_accounts_http_[0-9a-f]{12}\Z")


def upgrade() -> None:
    conn = op.get_bind()
    db = conn.scalar(sa.text("SELECT current_database()"))
    if db != "kpdesk":
        if not isinstance(db, str) or not _CLONE.fullmatch(db):
            raise RuntimeError("A20 must target working KPDesk or a marked clone.")
        marker = conn.scalar(sa.text("""
            SELECT shobj_description(oid,'pg_database')
            FROM pg_database WHERE datname=:database
        """), {"database": db})
        if marker != "kpdesk:accounts-http:" + db.rsplit("_", 1)[-1]:
            raise RuntimeError("A20 cloned database provenance failed.")
    if conn.scalar(sa.text("SELECT current_user")) != "kpdesk":
        raise RuntimeError("A20 requires the reviewed PostgreSQL owner.")
    if conn.scalar(sa.text("SELECT version_num FROM alembic_version")) != (
        "0018_email_team_onboarding"
    ):
        raise RuntimeError("A20 requires revision 0018.")
    if conn.scalar(sa.text(
        "SELECT to_regclass('public.tenant_llm_attempts')"
    )) is not None:
        raise RuntimeError("LLM quota table already exists.")

    op.create_table(
        "tenant_llm_attempts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("model", sa.String(50), nullable=False),
        sa.Column("input_chars", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(12), nullable=False,
                  server_default=sa.text("'reserved'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"],
                                ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("input_chars BETWEEN 1 AND 12000",
                           name="ck_llm_input_bound"),
        sa.CheckConstraint("status IN ('reserved','success','failed')",
                           name="ck_llm_status"),
        sa.CheckConstraint(
            "model IN ('openai/gpt-oss-120b','openai/gpt-oss-20b',"
            "'qwen/qwen3.8-27b')",
            name="ck_llm_model",
        ),
    )
    op.create_index("ix_llm_quota_org_time", "tenant_llm_attempts",
                    ["organization_id", "created_at"])
    op.create_index("ix_llm_quota_user_time", "tenant_llm_attempts",
                    ["user_id", "created_at"])
    op.execute("""
        REVOKE ALL ON public.tenant_llm_attempts
        FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime
    """)

    op.execute("""
        CREATE FUNCTION public.kpdesk_llm_reserve(
            p_org uuid,p_id uuid,p_model text,p_chars integer
        ) RETURNS text LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        DECLARE
            v_user uuid;
            v_now timestamptz;
            v_count bigint;
            v_chars bigint;
        BEGIN
            IF p_org IS NULL OR p_id IS NULL OR p_model IS NULL
                OR p_model NOT IN (
                    'openai/gpt-oss-120b','openai/gpt-oss-20b',
                    'qwen/qwen3.8-27b'
                ) OR p_chars IS NULL OR p_chars NOT BETWEEN 1 AND 12000
            THEN RETURN 'invalid'; END IF;
            IF current_setting('kpdesk.session_sha256',true)
                   !~ '^[0-9a-f]{64}$'
            THEN RETURN 'forbidden'; END IF;
            -- The caller binds auth session + CSRF into a transaction-local
            -- setting via the existing A14 tenant authorization function.
            -- Serializing by org protects call quotas from concurrent races.
            PERFORM 1 FROM public.organizations o
            WHERE o.id=p_org AND o.status='active' FOR UPDATE;
            IF NOT FOUND OR NOT public.kpdesk_session_tenant_allowed(
                p_org,'write'
            ) THEN RETURN 'forbidden'; END IF;
            SELECT s.user_id INTO v_user FROM public.auth_sessions s
            JOIN public.users u ON u.id=s.user_id
            JOIN public.organization_memberships m
              ON m.user_id=s.user_id AND m.organization_id=p_org
            WHERE s.token_sha256=current_setting('kpdesk.session_sha256',true)
              AND s.revoked_at IS NULL
              AND s.expires_at>clock_timestamp()
              AND u.status='active' AND u.email_verified_at IS NOT NULL
              AND m.status='active' AND m.role IN ('owner','editor');
            IF v_user IS NULL THEN RETURN 'forbidden'; END IF;
            v_now:=clock_timestamp();
            SELECT count(*),COALESCE(sum(input_chars),0)
                INTO v_count,v_chars
            FROM public.tenant_llm_attempts
            WHERE organization_id=p_org
              AND created_at>v_now-interval '1 day';
            IF v_count>=40 OR v_chars+p_chars>120000
            THEN RETURN 'organization_quota'; END IF;
            SELECT count(*) INTO v_count
            FROM public.tenant_llm_attempts
            WHERE user_id=v_user
              AND created_at>v_now-interval '1 hour';
            IF v_count>=6 THEN RETURN 'user_quota'; END IF;
            SELECT count(*) INTO v_count
            FROM public.tenant_llm_attempts
            WHERE organization_id=p_org AND status='reserved'
              AND lease_expires_at>v_now;
            IF v_count>=2 THEN RETURN 'organization_busy'; END IF;
            SELECT count(*) INTO v_count
            FROM public.tenant_llm_attempts
            WHERE user_id=v_user AND status='reserved'
              AND lease_expires_at>v_now;
            IF v_count>=1 THEN RETURN 'user_busy'; END IF;
            INSERT INTO public.tenant_llm_attempts(
                id,organization_id,user_id,model,input_chars,
                lease_expires_at
            ) VALUES(p_id,p_org,v_user,p_model,p_chars,v_now+interval '45 seconds');
            RETURN 'reserved';
        END $$;
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_llm_finish(
            p_id uuid,p_digest text,p_success boolean
        ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        BEGIN
            IF p_id IS NULL OR p_digest IS NULL
              OR p_digest !~ '^[0-9a-f]{64}$' OR p_success IS NULL
            THEN RETURN FALSE; END IF;
            UPDATE public.tenant_llm_attempts a SET
              status=CASE WHEN p_success THEN 'success' ELSE 'failed' END,
              finished_at=clock_timestamp()
            WHERE a.id=p_id AND a.status='reserved'
              AND EXISTS(
                SELECT 1 FROM public.auth_sessions s
                WHERE s.token_sha256=p_digest AND s.user_id=a.user_id
              );
            RETURN FOUND;
        END $$;
    """)
    for signature in (
        "kpdesk_llm_reserve(uuid,uuid,text,integer)",
        "kpdesk_llm_finish(uuid,text,boolean)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
        op.execute(
            f"GRANT EXECUTE ON FUNCTION public.{signature} "
            "TO kpdesk_tenant_runtime"
        )


def downgrade() -> None:
    raise RuntimeError("A20 inference metering ledger requires reviewed retention/backup.")
