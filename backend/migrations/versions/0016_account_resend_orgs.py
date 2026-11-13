"""A16 verified-password resend and session-scoped organization discovery.

Revision ID: 0016_account_resend_orgs
Revises: 0015_verified_accounts

Does not alter or relax existing tenant RLS or expose plaintext challenges.
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op

revision = "0016_account_resend_orgs"
down_revision = "0015_verified_accounts"
branch_labels = None
depends_on = None

_CLONE = re.compile(r"kpdesk_accounts_http_[0-9a-f]{12}\Z")


def upgrade() -> None:
    conn = op.get_bind()
    db = conn.scalar(sa.text("SELECT current_database()"))
    if db != "kpdesk":
        if not isinstance(db, str) or _CLONE.fullmatch(db) is None:
            raise RuntimeError("0016 must target working KPDesk or a marked clone.")
        label = conn.scalar(sa.text("""
            SELECT shobj_description(oid,'pg_database')
            FROM pg_database WHERE datname=:db
        """), {"db": db})
        if label != "kpdesk:accounts-http:" + db.rsplit("_", 1)[-1]:
            raise RuntimeError("0016 clone provenance was not verified.")
    if conn.scalar(sa.text("SELECT current_user")) != "kpdesk":
        raise RuntimeError("0016 requires the verified local schema owner.")
    if conn.scalar(sa.text("SELECT version_num FROM alembic_version")) != (
        "0015_verified_accounts"
    ):
        raise RuntimeError("0016 must follow 0015.")
    for name in ("kpdesk_account_resend_credential(text,text)",
                 "kpdesk_account_resend_issue(text,text,text,text)",
                 "kpdesk_user_organizations(text)"):
        if conn.scalar(sa.text("SELECT to_regprocedure(:name)"),
                       {"name": "public." + name}) is not None:
            raise RuntimeError("Unexpected existing account/organization function.")
    op.execute("""
        CREATE FUNCTION public.kpdesk_account_resend_credential(
            p_email text,p_peer text
        ) RETURNS text LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        DECLARE
            candidate text;
        BEGIN
            IF p_email IS NULL OR length(p_email)>254
                OR p_email<>lower(trim(p_email))
            THEN RETURN NULL; END IF;
            -- Same 15-minute account/peer rate-limit as signup;
            -- reject repetitive credential hash extraction.
            IF NOT public.kpdesk_account_limit('signup',p_email,p_peer)
            THEN RETURN NULL; END IF;
            SELECT u.password_hash INTO candidate
            FROM public.users u WHERE u.email=p_email
                AND u.status='pending' AND u.email_verified_at IS NULL;
            RETURN candidate;
        END $$;
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_account_resend_issue(
            p_email text,p_password_hash text,p_digest text,p_cipher text
        ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        DECLARE
            pending_user uuid;
            previous_created timestamptz;
            previous_org text;
            next_id uuid := gen_random_uuid();
        BEGIN
            IF p_email IS NULL OR length(p_email)>254
                OR p_email<>lower(trim(p_email))
                OR p_password_hash IS NULL OR length(p_password_hash)>512
                OR p_digest IS NULL OR p_digest !~ '^[0-9a-f]{64}$'
                OR p_cipher IS NULL OR length(p_cipher) NOT BETWEEN 80 AND 4096
            THEN RETURN FALSE; END IF;
            SELECT u.id INTO pending_user
            FROM public.users u
            WHERE u.email=p_email AND u.password_hash=p_password_hash
                AND u.status='pending' AND u.email_verified_at IS NULL
            FOR UPDATE;
            IF NOT FOUND THEN RETURN FALSE; END IF;

            SELECT c.created_at,c.organization_name
            INTO previous_created,previous_org
            FROM public.account_email_challenges c
            WHERE c.user_id=pending_user AND c.purpose='signup'
            ORDER BY c.created_at DESC,c.id DESC
            LIMIT 1 FOR UPDATE;
            IF NOT FOUND OR previous_created>clock_timestamp()-interval '60 seconds'
            THEN RETURN FALSE; END IF;
            -- All older tokens, whether expired or not, immediately lose
            -- verification authority. A fresh token never reuses their digest.
            UPDATE public.account_email_challenges
              SET consumed_at=clock_timestamp()
              WHERE user_id=pending_user AND purpose='signup'
                AND consumed_at IS NULL;
            INSERT INTO public.account_email_challenges(
                id,user_id,purpose,token_sha256,organization_name,expires_at
            ) VALUES (
                next_id,pending_user,'signup',p_digest,previous_org,
                clock_timestamp()+interval '30 minutes'
            );
            INSERT INTO public.account_email_outbox(
                challenge_id,recipient_email,purpose,token_ciphertext
            ) VALUES (next_id,p_email,'signup',p_cipher);
            RETURN TRUE;
        END $$;
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_user_organizations(p_session_hash text)
        RETURNS TABLE(organization_id uuid,organization_name text,
                      membership_role text)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
          SELECT o.id,o.name::text,m.role::text
          FROM public.auth_sessions s
          JOIN public.users u ON u.id=s.user_id
          JOIN public.organization_memberships m ON m.user_id=u.id
          JOIN public.organizations o ON o.id=m.organization_id
          WHERE p_session_hash ~ '^[0-9a-f]{64}$'
            AND s.token_sha256=p_session_hash AND s.revoked_at IS NULL
            AND s.expires_at>statement_timestamp()
            AND u.status='active' AND u.email_verified_at IS NOT NULL
            AND m.status='active' AND o.status='active'
          ORDER BY o.name,o.id
        $$;
    """)
    for signature, role in (
        ("kpdesk_account_resend_credential(text,text)", "kpdesk_auth_runtime"),
        ("kpdesk_account_resend_issue(text,text,text,text)", "kpdesk_auth_runtime"),
        ("kpdesk_user_organizations(text)", "kpdesk_tenant_runtime"),
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{signature} TO {role}")


def downgrade() -> None:
    raise RuntimeError("A16 requires reviewed rollback; live account state is not disposable.")
