"""Live release: separate auth/tenant PostgreSQL roles and session-bound RLS.

Revision ID: 0014_live_auth_rls
Revises: 0013_live_expand

Compatible with the old owner-token API: kpdesk_app retains its existing
legacy read/write grants, but CANNOT mint auth sessions or read tenant tables.
Unreleased public session routes remain unmounted. New NOLOGIN roles are
explicitly provisioned with credentials only in a separate deployment step.
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0014_live_auth_rls"
down_revision = "0013_live_expand"
branch_labels = None
depends_on = None

_CLONE = re.compile(r"kpdesk_auth_live_e2e_[0-9a-f]{12}\Z")
_ROLES = ("kpdesk_auth_runtime", "kpdesk_tenant_runtime")
_DOMAIN = (
    "catalog_versions", "catalog_items", "idempotency_records",
    "quote_documents", "quote_revisions", "quote_write_requests",
)
_TENANT = (
    "tenant_catalog_active", "tenant_catalog_idempotency",
    "tenant_quote_write_requests",
)
_ALL = (*_DOMAIN, *_TENANT)


def _preflight(conn: sa.engine.Connection) -> None:
    name = conn.scalar(sa.text("SELECT current_database()"))
    if name != "kpdesk":
        if not isinstance(name, str) or not _CLONE.fullmatch(name):
            raise RuntimeError("Live auth migration refuses unknown database.")
        marker = conn.scalar(sa.text("""
            SELECT shobj_description(oid, 'pg_database')
            FROM pg_database WHERE datname=:name
        """), {"name": name})
        if marker != "kpdesk:auth-live-rehearsal:" + name.rsplit("_", 1)[-1]:
            raise RuntimeError("Live auth clone marker invalid.")
    if conn.scalar(sa.text("SELECT current_user")) != "kpdesk":
        raise RuntimeError("Live auth migration requires local schema owner.")
    if conn.scalar(sa.text("SELECT version_num FROM alembic_version")) != (
        "0013_live_expand"
    ):
        raise RuntimeError("Live auth migration requires applied 0013.")
    if conn.execute(sa.text("""
        SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole
        FROM pg_roles WHERE rolname='kpdesk_app'
    """)).one_or_none() != (False, False, False, False):
        raise RuntimeError("Legacy runtime has unsafe privileges.")
    for role in _ROLES:
        if conn.scalar(sa.text("""
            SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:name)
        """), {"name": role}):
            raise RuntimeError("Auth/tenant runtime role already exists.")
    if conn.scalar(sa.text("SELECT to_regclass('public.auth_sessions')")):
        raise RuntimeError("Auth sessions already provisioned.")
    for table in _TENANT:
        if conn.scalar(sa.text("SELECT to_regclass(:table) IS NOT NULL"),
                       {"table": "public." + table}) is not True:
            raise RuntimeError("Missing 0013 tenant table.")
    # Never attach a new security regime to unforeseen prior customer data.
    for table in ("users", "organizations", "organization_memberships",
                  "catalog_versions", "quote_documents"):
        if conn.scalar(sa.text(f"SELECT count(*) FROM {table}")):
            raise RuntimeError(f"Unreviewed existing users/business data: {table}")


def upgrade() -> None:
    conn = op.get_bind()
    _preflight(conn)
    op.create_table(
        "auth_sessions",
        sa.Column("token_sha256", sa.String(64), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("csrf_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_auth_sessions_user",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "token_sha256 ~ '^[0-9a-f]{64}$' "
            "AND csrf_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_auth_session_sha256",
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name="ck_auth_session_valid_lifetime",
        ),
    )
    op.create_index(
        "ix_auth_sessions_user_active", "auth_sessions",
        ["user_id", "expires_at"], postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_table(
        "auth_login_limits",
        sa.Column("bucket", sa.String(80), primary_key=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.CheckConstraint("attempts BETWEEN 1 AND 2147483647",
                           name="ck_auth_login_attempts"),
    )
    for role in _ROLES:
        op.execute(
            f"CREATE ROLE {role} NOLOGIN NOINHERIT NOSUPERUSER "
            "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
        )
        op.execute(f"GRANT USAGE ON SCHEMA public TO {role}")

    # Authentication functions live under a strictly separate NOLOGIN service
    # identity. The old catalog process cannot call credential lookup or revoke.
    op.execute("""
        CREATE FUNCTION public.kpdesk_auth_credential(p_email text, p_peer text)
        RETURNS TABLE(identity_id uuid, secret_hash text,
                      usable boolean, limited boolean)
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        DECLARE
            observed timestamptz := clock_timestamp();
            account_bucket text;
            peer_bucket text;
            account_count integer;
            peer_count integer;
        BEGIN
            IF p_email IS NULL OR length(p_email)>254
               OR p_peer IS NULL OR length(p_peer)>64
            THEN
                RAISE EXCEPTION 'invalid authentication request';
            END IF;
            account_bucket := 'a:' || encode(sha256(convert_to(p_email,'UTF8')),'hex');
            peer_bucket := 'p:' || encode(sha256(convert_to(p_peer,'UTF8')),'hex');
            INSERT INTO public.auth_login_limits(bucket,window_start,attempts)
            VALUES (LEAST(account_bucket,peer_bucket),observed,1)
            ON CONFLICT (bucket) DO UPDATE SET
                attempts=CASE WHEN public.auth_login_limits.window_start
                    < observed - interval '15 minutes' THEN 1
                    ELSE public.auth_login_limits.attempts + 1 END,
                window_start=CASE WHEN public.auth_login_limits.window_start
                    < observed - interval '15 minutes' THEN observed
                    ELSE public.auth_login_limits.window_start END;
            INSERT INTO public.auth_login_limits(bucket,window_start,attempts)
            VALUES (GREATEST(account_bucket,peer_bucket),observed,1)
            ON CONFLICT (bucket) DO UPDATE SET
                attempts=CASE WHEN public.auth_login_limits.window_start
                    < observed - interval '15 minutes' THEN 1
                    ELSE public.auth_login_limits.attempts + 1 END,
                window_start=CASE WHEN public.auth_login_limits.window_start
                    < observed - interval '15 minutes' THEN observed
                    ELSE public.auth_login_limits.window_start END;
            SELECT attempts INTO account_count FROM public.auth_login_limits
            WHERE bucket=account_bucket;
            SELECT attempts INTO peer_count FROM public.auth_login_limits
            WHERE bucket=peer_bucket;
            IF account_count>6 OR peer_count>30 THEN
                RETURN QUERY SELECT NULL::uuid,NULL::text,false,true;
                RETURN;
            END IF;
            RETURN QUERY SELECT u.id,u.password_hash::text,
                (u.status='active' AND u.email_verified_at IS NOT NULL),false
            FROM public.users u WHERE u.email=p_email;
        END $$;
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_auth_session(p_token_hash text)
        RETURNS TABLE(identity_id uuid, csrf_hash text)
        LANGUAGE sql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
            SELECT s.user_id,s.csrf_sha256
            FROM public.auth_sessions s JOIN public.users u ON u.id=s.user_id
            WHERE s.token_sha256=p_token_hash AND s.revoked_at IS NULL
              AND s.expires_at>clock_timestamp()
              AND u.status='active' AND u.email_verified_at IS NOT NULL
        $$;
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_auth_revoke(p_token_hash text)
        RETURNS boolean LANGUAGE sql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
            WITH revoked AS (
                UPDATE public.auth_sessions SET revoked_at=clock_timestamp()
                WHERE token_sha256=p_token_hash AND revoked_at IS NULL
                  AND expires_at>clock_timestamp() RETURNING token_sha256
            )
            SELECT EXISTS(SELECT 1 FROM revoked)
        $$;
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_session_tenant_allowed(
            p_org uuid, p_action text
        ) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
            SELECT COALESCE(
                p_org IS NOT NULL
                AND p_action IN ('read','write','manage')
                AND current_setting('kpdesk.session_sha256',true)
                    ~ '^[0-9a-f]{64}$'
                AND EXISTS (
                    SELECT 1 FROM public.auth_sessions s
                    JOIN public.users u ON u.id=s.user_id
                    JOIN public.organization_memberships m
                      ON m.user_id=u.id AND m.organization_id=p_org
                    JOIN public.organizations o ON o.id=m.organization_id
                    WHERE s.token_sha256=
                          current_setting('kpdesk.session_sha256',true)
                      AND s.revoked_at IS NULL
                      AND s.expires_at>statement_timestamp()
                      AND u.status='active' AND u.email_verified_at IS NOT NULL
                      AND o.status='active' AND m.status='active'
                      AND (p_action='read'
                        OR (p_action='write' AND m.role IN ('owner','editor'))
                        OR (p_action='manage' AND m.role='owner'))
                ), false)
        $$;
    """)
    signatures = (
        "kpdesk_auth_credential(text,text)", "kpdesk_auth_session(text)",
        "kpdesk_auth_revoke(text)", "kpdesk_session_tenant_allowed(uuid,text)",
    )
    for signature in signatures:
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
    for signature in signatures[:3]:
        op.execute(
            f"GRANT EXECUTE ON FUNCTION public.{signature} TO kpdesk_auth_runtime"
        )
    for signature in (signatures[1], signatures[3]):
        op.execute(
            f"GRANT EXECUTE ON FUNCTION public.{signature} TO kpdesk_tenant_runtime"
        )
    op.execute("GRANT INSERT ON public.auth_sessions TO kpdesk_auth_runtime")
    # Old kpdesk_app receives NO new credential/auth/tenant grants.
    for table in _DOMAIN:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY legacy_owner_{table} ON public.{table} "
            "FOR ALL TO kpdesk_app USING (true) WITH CHECK (true)"
        )
    for table in _ALL:
        if table in _TENANT:
            op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY session_select_{table} ON public.{table} "
            "FOR SELECT TO kpdesk_tenant_runtime "
            "USING (public.kpdesk_session_tenant_allowed(organization_id,'read'))"
        )
        op.execute(
            f"CREATE POLICY session_insert_{table} ON public.{table} "
            "FOR INSERT TO kpdesk_tenant_runtime "
            "WITH CHECK (public.kpdesk_session_tenant_allowed(organization_id,'write'))"
        )
        op.execute(
            f"CREATE POLICY session_update_{table} ON public.{table} "
            "FOR UPDATE TO kpdesk_tenant_runtime "
            "USING (public.kpdesk_session_tenant_allowed(organization_id,'write')) "
            "WITH CHECK (public.kpdesk_session_tenant_allowed(organization_id,'write'))"
        )
        op.execute(f"GRANT SELECT, INSERT ON public.{table} TO kpdesk_tenant_runtime")
    # Only mutable heads/pointers can be updated. No DELETE or snapshot UPDATE.
    for table in ("quote_documents", "tenant_catalog_active"):
        op.execute(f"GRANT UPDATE ON public.{table} TO kpdesk_tenant_runtime")
    # The old global process is NOT given SELECT/UPDATE rights to new tenant
    # tables; its existing catalog/quote access remains unchanged.


def downgrade() -> None:
    raise RuntimeError(
        "Auth/RLS release downgrade is not automatic; restore a verified backup "
        "and explicitly revoke and clean service roles."
    )
