"""Dry-run the ACTUAL 0014 auth/RLS migration on a marked 0013 clone.

Never changes real kpdesk; DDL (including NOLOGIN roles) is rolled back.
"""
from __future__ import annotations

import importlib
import secrets
import subprocess
from pathlib import Path

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from dotenv import dotenv_values
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool

from scripts.rehearse_release_snapshot import _docker_input

ROOT = Path(__file__).resolve().parents[2]
VERSION = "0013_live_expand"
TEST_DBS = (
    "auth_sessions", "auth_login_limits",
)
_ROLES = ("kpdesk_auth_runtime", "kpdesk_tenant_runtime")


def _engine(database: str, password: str, port: int, *, admin: bool = False) -> Engine:
    return create_engine(
        URL.create("postgresql+psycopg", username="kpdesk", password=password,
                   host="127.0.0.1", port=port, database=database),
        isolation_level="AUTOCOMMIT" if admin else None,
        poolclass=NullPool, pool_pre_ping=True,
        connect_args={"connect_timeout": 4},
    )


def _assert_clear_before(conn: Connection) -> None:
    if conn.scalar(text("SELECT version_num FROM alembic_version")) != VERSION:
        raise RuntimeError("Live auth source requires 0013.")
    for role in _ROLES:
        if conn.scalar(text(
            "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:role)"
        ), {"role": role}):
            raise RuntimeError("Auth roles already present: migration may be applied.")
    for table in TEST_DBS:
        if conn.scalar(text("SELECT to_regclass(:name)"),
                       {"name": "public." + table}) is not None:
            raise RuntimeError("Unexpected existing auth tables.")


def verify() -> None:
    env = dotenv_values(ROOT / ".env")
    password = env.get("KP_DESK_DB_PASSWORD")
    if not isinstance(password, str) or len(password) < 32:
        raise RuntimeError("Missing owner credentials.")
    port = int(env.get("KP_DESK_DB_PORT") or 55432)
    nonce = secrets.token_hex(6)
    clone_name = "kpdesk_auth_live_e2e_" + nonce
    marker = "kpdesk:auth-live-rehearsal:" + nonce
    admin = _engine("postgres", password, port, admin=True)
    source = _engine("kpdesk", password, port)
    clone: Engine | None = None
    created = False
    dump = ROOT / "backups" / "2026-10-10-a14-auth-restore" / (nonce + ".dump")
    dump.parent.mkdir(parents=True, exist_ok=True)
    try:
        with source.connect() as conn:
            _assert_clear_before(conn)
            if conn.scalar(text("SELECT count(*) FROM users")):
                raise RuntimeError("Unexpected live users.")
        # PostgreSQL forbids TEMPLATE=kpdesk while web/test clients are
        # connected. A consistent pg_dump avoids disconnecting active users.
        with dump.open("xb") as archive:
            subprocess.run(
                ["docker", "exec", "kpdesk-db-1", "pg_dump", "-U", "kpdesk",
                 "-d", "kpdesk", "-Fc", "--no-owner", "--no-acl",
                 "--serializable-deferrable"],
                stdout=archive, stderr=subprocess.PIPE, check=True, timeout=120,
            )
        with admin.connect() as conn:
            if conn.scalar(text("SELECT current_database()")) != "postgres":
                raise RuntimeError("Wrong admin DB.")
            if conn.scalar(text("""
                SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=:name)
            """), {"name": clone_name}):
                raise RuntimeError("Generated clone name occupied.")
            conn.exec_driver_sql(
                f"CREATE DATABASE {clone_name} TEMPLATE template0"
            )
            created = True
            conn.exec_driver_sql(
                f"COMMENT ON DATABASE {clone_name} IS '{marker}'"
            )
        _docker_input(
            "pg_restore", "-U", "kpdesk", "--dbname=" + clone_name,
            "--exit-on-error", "--single-transaction",
            "--no-owner", "--no-acl", input_file=dump,
        )
        clone = _engine(clone_name, password, port)
        with clone.connect() as conn:
            tx = conn.begin()
            try:
                with Operations.context(MigrationContext.configure(conn)):
                    importlib.import_module(
                        "migrations.versions.0014_live_auth_rls"
                    ).upgrade()
                for table in TEST_DBS:
                    if conn.scalar(
                        text("SELECT to_regclass(:name) IS NOT NULL"),
                        {"name": "public." + table},
                    ) is not True:
                        raise RuntimeError("Auth schema incomplete.")
                for role in _ROLES:
                    item = conn.execute(text("""
                        SELECT rolcanlogin, rolsuper, rolbypassrls
                        FROM pg_roles WHERE rolname=:name
                    """), {"name": role}).one()
                    if item != (False, False, False):
                        raise RuntimeError("Runtime role grants are unsafe.")
                for name in (
                    "kpdesk_auth_credential(text,text)",
                    "kpdesk_auth_session(text)",
                    "kpdesk_auth_revoke(text)",
                    "kpdesk_session_tenant_allowed(uuid,text)",
                ):
                    if conn.scalar(text("""
                        SELECT to_regprocedure(:sig) IS NOT NULL
                    """), {"sig": "public." + name}) is not True:
                        raise RuntimeError("Auth gateway missing.")
                for role in ("kpdesk_app", "kpdesk_tenant_runtime"):
                    if conn.scalar(text("""
                        SELECT has_function_privilege(
                            :role,
                            'public.kpdesk_auth_credential(text,text)',
                            'EXECUTE')
                    """), {"role": role}) is not False:
                        raise RuntimeError("Credential API exposed to catalog runtime.")
                if conn.scalar(text("""
                    SELECT has_function_privilege(
                        'kpdesk_auth_runtime',
                        'public.kpdesk_auth_credential(text,text)',
                        'EXECUTE')
                """)) is not True:
                    raise RuntimeError("Auth service cannot verify credentials.")
                if conn.scalar(text("""
                    SELECT has_table_privilege('kpdesk_app',
                        'public.tenant_catalog_active','SELECT')
                """)) is not False:
                    raise RuntimeError("Old global API gained tenant permissions.")
                for table in (
                    "tenant_catalog_active", "tenant_catalog_idempotency",
                    "tenant_quote_write_requests",
                ):
                    if conn.execute(text("""
                        SELECT relrowsecurity, relforcerowsecurity FROM pg_class
                        WHERE oid=to_regclass(:table)
                    """), {"table": "public." + table}).one() != (True, True):
                        raise RuntimeError("New tenant table missing FORCE RLS.")
                if conn.scalar(text("""
                    SELECT public.kpdesk_session_tenant_allowed(
                        '00000000-0000-0000-0000-000000000000'::uuid,'write')
                """)) is not False:
                    raise RuntimeError("Missing session was not denied.")
            finally:
                tx.rollback()
            if conn.scalar(text("""
                SELECT to_regclass('public.auth_sessions')
            """)) is not None:
                raise RuntimeError("Test migration wasn't fully rolled back.")
        with source.connect() as conn:
            _assert_clear_before(conn)
        print(
            "LIVE_AUTH_RLS_REHEARSAL_PASSED "
            "auth_role_separated=true legacy_api_unchanged=true "
            "rls_force_on_tenant_tables=true no_session_denied=true "
            "migration_rolled_back=true"
        )
    finally:
        if clone is not None:
            clone.dispose()
        try:
            if created:
                with admin.connect() as conn:
                    actual = conn.scalar(text("""
                        SELECT shobj_description(oid,'pg_database')
                        FROM pg_database WHERE datname=:name
                    """), {"name": clone_name})
                    if actual != marker:
                        raise RuntimeError("Refusing deletion of unmarked DB.")
                    conn.exec_driver_sql(
                        f"DROP DATABASE {clone_name} WITH (FORCE)"
                    )
        finally:
            admin.dispose()
            source.dispose()
            if dump.exists():
                dump.unlink()


if __name__ == "__main__":
    verify()
