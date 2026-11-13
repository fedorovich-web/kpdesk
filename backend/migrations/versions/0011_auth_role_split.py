"""A7 isolated runtime split: catalog can validate but cannot mint auth sessions.

Revision ID: 0011_auth_role_split
Revises: 0010_auth_gateway

This is ONLY transactionally rehearsed on kpdesk_tenant_test. CREATE ROLE is
rolled back with the same test transaction; no production privileges or role
credentials are altered. This no-login test role is not a deployed auth service.
Also accepts a temporary nonce-named, owner-comment-marked clone of
kpdesk_tenant_test used exclusively for physical-connection integration tests.
Production kpdesk is NEVER an allowed target.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from kpdesk.db.test_database_scope import is_auth_test_database

revision = "0011_auth_role_split"
down_revision = "0010_auth_gateway"
branch_labels = None
depends_on = None

_AUTH_ROLE = "kpdesk_auth_test"


def upgrade() -> None:
    connection = op.get_bind()
    if not is_auth_test_database(connection):
        raise RuntimeError("A7 auth role migration forbidden outside kpdesk_tenant_test.")
    if connection.scalar(sa.text("SELECT version_num FROM alembic_version")) != (
        "0006_legacy_quarantine"
    ):
        raise RuntimeError("A7 requires an isolated A2 baseline transaction.")
    if connection.scalar(sa.text("SELECT current_user")) != "kpdesk":
        raise RuntimeError("A7 must run as expected test schema owner.")
    for name in (
        "kpdesk_auth_credential(text,text)", "kpdesk_auth_session(text)",
        "kpdesk_auth_revoke(text)", "kpdesk_auth_member(uuid,uuid)",
    ):
        if connection.scalar(sa.text(
            "SELECT to_regprocedure(:procedure) IS NOT NULL"
        ), {"procedure": "public." + name}) is not True:
            raise RuntimeError("A6 gateway function is not installed.")
    if connection.scalar(sa.text(
        "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:role)"
    ), {"role": _AUTH_ROLE}):
        raise RuntimeError("Isolated auth test role already exists.")
    op.execute("CREATE ROLE kpdesk_auth_test NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS")
    op.execute("GRANT USAGE ON SCHEMA public TO kpdesk_auth_test")
    op.execute("REVOKE INSERT ON TABLE public.auth_sessions FROM kpdesk_app")
    op.execute("REVOKE EXECUTE ON FUNCTION public.kpdesk_auth_credential(text,text) FROM kpdesk_app")
    op.execute("REVOKE EXECUTE ON FUNCTION public.kpdesk_auth_revoke(text) FROM kpdesk_app")
    op.execute("GRANT EXECUTE ON FUNCTION public.kpdesk_auth_credential(text,text) TO kpdesk_auth_test")
    op.execute("GRANT EXECUTE ON FUNCTION public.kpdesk_auth_revoke(text) TO kpdesk_auth_test")
    op.execute("GRANT EXECUTE ON FUNCTION public.kpdesk_auth_session(text) TO kpdesk_auth_test")
    op.execute("GRANT INSERT ON TABLE public.auth_sessions TO kpdesk_auth_test")
    # Neither role receives direct SELECT on users, sessions or login limits.


def downgrade() -> None:
    raise RuntimeError("A7 auth role test must roll back as one transaction.")
