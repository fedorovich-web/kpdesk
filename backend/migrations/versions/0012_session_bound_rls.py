"""A8 authenticated PostgreSQL RLS: no SQL-client-controlled org context.

Revision ID: 0012_session_bound_rls
Revises: 0011_auth_role_split

TEST-ONLY, strict isolated DB marker checks; executed inside rollback-only
kpdesk_tenant_test transactions and temporary owner-marked clone rehearsal.
Never remove production guards or apply to existing kpdesk API schema directly.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from kpdesk.db.test_database_scope import is_auth_test_database

revision = "0012_session_bound_rls"
down_revision = "0011_auth_role_split"
branch_labels = None
depends_on = None

# All tenant-owned tables from A3 + supplier approval history from A4.
_TENANT_TABLES = (
    "catalog_versions",
    "catalog_items",
    "catalog_active",
    "idempotency_records",
    "quote_documents",
    "quote_revisions",
    "quote_write_requests",
    "supplier_reviewed_profiles",
    "supplier_review_decisions",
)


def upgrade() -> None:
    connection = op.get_bind()
    if not is_auth_test_database(connection):
        raise RuntimeError("A8 migration forbidden outside isolated tenant test databases.")
    if connection.scalar(sa.text("SELECT version_num FROM alembic_version")) != (
        "0006_legacy_quarantine"
    ):
        raise RuntimeError("A8 requires an unchanged A2 migration baseline.")
    if connection.scalar(sa.text("SELECT current_user")) != "kpdesk":
        raise RuntimeError("A8 requires the expected test schema owner.")
    functions = (
        "kpdesk_auth_credential(text,text)",
        "kpdesk_auth_session(text)",
        "kpdesk_auth_revoke(text)",
        "kpdesk_auth_member(uuid,uuid)",
    )
    for function in functions:
        if connection.scalar(sa.text("""
            SELECT to_regprocedure(:name) IS NOT NULL
        """), {"name": "public." + function}) is not True:
            raise RuntimeError("The audited auth gateway A6 must exist.")
    for table in _TENANT_TABLES:
        result = connection.execute(sa.text("""
            SELECT relrowsecurity, relforcerowsecurity
            FROM pg_class WHERE oid=to_regclass(:table)
        """), {"table": "public." + table}).one_or_none()
        if result != (True, True):
            raise RuntimeError("A3/A4 FORCE RLS tenant contract missing.")

    # A security-definer validator must consult the *active session* stored in
    # PostgreSQL, not a user-id or org GUC supplied by potentially compromised
    # application SQL. A guessed/invalid/expired/revoked token SHA always denies.
    # Status changes in users/orgs/memberships take effect on the next statement.
    op.execute("""
        CREATE FUNCTION public.kpdesk_session_tenant_allowed(
            p_org uuid, p_action text
        ) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $$
            SELECT COALESCE(
                p_org IS NOT NULL
                AND p_action IN ('read', 'write', 'manage')
                AND current_setting('kpdesk.session_sha256', true)
                    ~ '^[0-9a-f]{64}$'
                AND EXISTS (
                    SELECT 1
                    FROM public.auth_sessions AS s
                    JOIN public.users AS u ON u.id=s.user_id
                    JOIN public.organization_memberships AS m
                         ON m.user_id=u.id AND m.organization_id=p_org
                    JOIN public.organizations AS o ON o.id=m.organization_id
                    WHERE s.token_sha256 =
                        current_setting('kpdesk.session_sha256', true)
                      AND s.expires_at > statement_timestamp()
                      AND s.revoked_at IS NULL
                      AND u.status='active'
                      AND u.email_verified_at IS NOT NULL
                      AND o.status='active'
                      AND m.status='active'
                      AND (
                        p_action='read'
                        OR (p_action='write' AND m.role IN ('owner', 'editor'))
                        OR (p_action='manage' AND m.role='owner')
                      )
                ), FALSE)
        $$;
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.kpdesk_session_tenant_allowed(uuid,text) "
        "FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.kpdesk_session_tenant_allowed(uuid,text) "
        "TO kpdesk_app"
    )
    # Membership must be derived from the bound session. Calling the previous
    # identity-by-UUID gateway cannot authorize data or enumerate members.
    op.execute(
        "REVOKE EXECUTE ON FUNCTION public.kpdesk_auth_member(uuid,uuid) "
        "FROM kpdesk_app"
    )
    for table in _TENANT_TABLES:
        op.execute(f"DROP POLICY tenant_access_{table} ON public.{table}")
        op.execute(
            f"CREATE POLICY tenant_read_{table} ON public.{table} "
            "FOR SELECT TO kpdesk_app "
            "USING (public.kpdesk_session_tenant_allowed(organization_id, 'read'))"
        )
        op.execute(
            f"CREATE POLICY tenant_insert_{table} ON public.{table} "
            "FOR INSERT TO kpdesk_app "
            "WITH CHECK (public.kpdesk_session_tenant_allowed(organization_id, 'write'))"
        )
        op.execute(
            f"CREATE POLICY tenant_update_{table} ON public.{table} "
            "FOR UPDATE TO kpdesk_app "
            "USING (public.kpdesk_session_tenant_allowed(organization_id, 'write')) "
            "WITH CHECK (public.kpdesk_session_tenant_allowed(organization_id, 'write'))"
        )
        # Deliberately NO DELETE policy. Immutable rows also have no UPDATE grants.


def downgrade() -> None:
    raise RuntimeError("A8 authenticated RLS must roll back in its entire test transaction.")
