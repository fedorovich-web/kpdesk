"""Test-only least-privilege login/session/membership PostgreSQL gateway.

Revision ID: 0010_auth_gateway
Revises: 0009_auth_sessions

Only transactionally usable on kpdesk_tenant_test, with rollback of DDL/grants.
The gateway is NOT a replacement for TLS, request-level rate limiting, strong
credential checks or a separately audited production migration.
Also accepts a temporary nonce-named, owner-comment-marked clone of
kpdesk_tenant_test used exclusively for physical-connection integration tests.
Production kpdesk is NEVER an allowed target.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from kpdesk.db.test_database_scope import is_auth_test_database

revision = "0010_auth_gateway"
down_revision = "0009_auth_sessions"
branch_labels = None
depends_on = None

_LOOKUP_SIG = "public.kpdesk_auth_credential(text,text)"
_SESSION_SIG = "public.kpdesk_auth_session(text)"
_REVOKE_SIG = "public.kpdesk_auth_revoke(text)"
_MEMBER_SIG = "public.kpdesk_auth_member(uuid,uuid)"


def upgrade() -> None:
    connection = op.get_bind()
    if not is_auth_test_database(connection):
        raise RuntimeError("Auth gateway migration forbidden outside kpdesk_tenant_test.")
    if connection.scalar(sa.text("SELECT version_num FROM alembic_version")) != (
        "0006_legacy_quarantine"
    ):
        raise RuntimeError("Auth gateway requires isolated A2 transaction.")
    for table in ("catalog_versions", "supplier_reviewed_profiles"):
        if connection.execute(sa.text("""
            SELECT relrowsecurity,relforcerowsecurity
            FROM pg_class WHERE oid=to_regclass(:table)
        """), {"table": table}).one_or_none() != (True, True):
            raise RuntimeError("A3/A4 RLS contract not present.")
    if connection.scalar(sa.text("SELECT to_regclass('public.auth_sessions')")) is None:
        raise RuntimeError("A5 auth table is missing.")
    if connection.scalar(sa.text("SELECT current_user")) != "kpdesk":
        raise RuntimeError("Expected test database schema owner.")

    op.create_table(
        "auth_login_limits",
        sa.Column("bucket", sa.String(80), primary_key=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.CheckConstraint("attempts > 0 AND attempts <= 2147483647",
                           name="ck_auth_login_attempts"),
        sa.CheckConstraint("length(bucket) BETWEEN 1 AND 80",
                           name="ck_auth_login_bucket"),
    )

    # Fail-closed PostgreSQL counters shared by concurrent workers, keyed on
    # account and source network address (never raw email/IP). A SQL client
    # can't reset one without owner privilege.
    op.execute("""
        CREATE FUNCTION public.kpdesk_auth_credential(
            p_email text, p_peer text
        ) RETURNS TABLE (
            identity_id uuid, secret_hash text,
            usable boolean, limited boolean
        )
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $$
        DECLARE
            now_at timestamptz := clock_timestamp();
            account_bucket text;
            peer_bucket text;
            account_tries integer;
            peer_tries integer;
        BEGIN
            IF p_email IS NULL OR length(p_email) > 254
                OR p_peer IS NULL OR length(p_peer) > 64
            THEN
                RAISE EXCEPTION 'invalid auth request';
            END IF;
            account_bucket := 'a:' || encode(sha256(convert_to(p_email, 'UTF8')), 'hex');
            peer_bucket := 'p:' || encode(sha256(convert_to(p_peer, 'UTF8')), 'hex');

            -- Deterministic lock order: account then peer would deadlock if
            -- callers target multiple accounts from different peers.
            -- Use sorted bucket IDs for stable lock order.
            INSERT INTO public.auth_login_limits(bucket,window_start,attempts)
              VALUES (LEAST(account_bucket,peer_bucket),now_at,1)
              ON CONFLICT (bucket) DO UPDATE SET
                attempts = CASE WHEN
                    public.auth_login_limits.window_start < now_at - interval '15 minutes'
                THEN 1 ELSE public.auth_login_limits.attempts + 1 END,
                window_start = CASE WHEN
                    public.auth_login_limits.window_start < now_at - interval '15 minutes'
                THEN now_at ELSE public.auth_login_limits.window_start END;
            INSERT INTO public.auth_login_limits(bucket,window_start,attempts)
              VALUES (GREATEST(account_bucket,peer_bucket),now_at,1)
              ON CONFLICT (bucket) DO UPDATE SET
                attempts = CASE WHEN
                    public.auth_login_limits.window_start < now_at - interval '15 minutes'
                THEN 1 ELSE public.auth_login_limits.attempts + 1 END,
                window_start = CASE WHEN
                    public.auth_login_limits.window_start < now_at - interval '15 minutes'
                THEN now_at ELSE public.auth_login_limits.window_start END;
            SELECT attempts INTO account_tries FROM public.auth_login_limits
              WHERE bucket=account_bucket;
            SELECT attempts INTO peer_tries FROM public.auth_login_limits
              WHERE bucket=peer_bucket;
            IF account_tries > 6 OR peer_tries > 30 THEN
                RETURN QUERY SELECT NULL::uuid,NULL::text,false,true;
                RETURN;
            END IF;
            RETURN QUERY
                SELECT u.id,u.password_hash::text,
                       (u.status='active' AND u.email_verified_at IS NOT NULL),
                       false
                FROM public.users AS u WHERE u.email=p_email;
        END $$;
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_auth_session(p_token_hash text)
        RETURNS TABLE(identity_id uuid,csrf_hash text)
        LANGUAGE sql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $$
            SELECT a.user_id,a.csrf_sha256
            FROM public.auth_sessions AS a
            JOIN public.users AS u ON u.id=a.user_id
            WHERE a.token_sha256=p_token_hash
              AND a.expires_at>clock_timestamp()
              AND a.revoked_at IS NULL AND u.status='active'
              AND u.email_verified_at IS NOT NULL
        $$;
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_auth_revoke(p_token_hash text)
        RETURNS boolean
        LANGUAGE sql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $$
            WITH updated AS (
                UPDATE public.auth_sessions
                SET revoked_at=clock_timestamp()
                WHERE token_sha256=p_token_hash AND revoked_at IS NULL
                  AND expires_at>clock_timestamp()
                RETURNING token_sha256
            )
            SELECT EXISTS (SELECT 1 FROM updated)
        $$;
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_auth_member(p_user uuid,p_org uuid)
        RETURNS text
        LANGUAGE sql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $$
            SELECT m.role::text FROM public.organization_memberships AS m
            JOIN public.organizations AS o ON o.id=m.organization_id
            JOIN public.users AS u ON u.id=m.user_id
            WHERE m.organization_id=p_org AND m.user_id=p_user
              AND m.status='active' AND o.status='active'
              AND u.status='active' AND u.email_verified_at IS NOT NULL
        $$;
    """)
    for signature in (_LOOKUP_SIG, _SESSION_SIG, _REVOKE_SIG, _MEMBER_SIG):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO kpdesk_app")

    # Explicitly NO SELECT on users, organization_memberships or auth_sessions
    # and NO app access to auth_login_limits. Only INSERT the new session token
    # hash is required. Statements in production must also use separate creds.
    op.execute("REVOKE ALL ON TABLE public.users FROM kpdesk_app")
    op.execute("REVOKE ALL ON TABLE public.organization_memberships FROM kpdesk_app")
    op.execute("REVOKE ALL ON TABLE public.organizations FROM kpdesk_app")
    op.execute("REVOKE ALL ON TABLE public.auth_sessions FROM kpdesk_app")
    op.execute("GRANT INSERT ON TABLE public.auth_sessions TO kpdesk_app")


def downgrade() -> None:
    raise RuntimeError("Auth gateway must be rolled back in one test transaction.")
