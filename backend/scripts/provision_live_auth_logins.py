"""Provision two genuinely independent local PostgreSQL service LOGIN identities.

Secrets are random and stored only in gitignored credentials/ with Windows
owner-only ACL. Refuse partial/mismatched prior provisioning. No user accounts
or organization data are fabricated. No public HTTP routes are enabled.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import Connection, create_engine, event, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[2]
SECRETS_FILE = ROOT / "credentials" / "kpdesk-runtime-db.json"
TARGETS = (
    ("kpdesk_auth_service", "kpdesk_auth_runtime"),
    ("kpdesk_tenant_service", "kpdesk_tenant_runtime"),
)
_PASSWORD = re.compile(r"[A-Za-z0-9_-]{48}\Z")


def _connect(database: str, user: str, password: str, port: int) -> Engine:
    return create_engine(
        URL.create(
            "postgresql+psycopg", username=user, password=password,
            host="127.0.0.1", port=port, database=database,
        ),
        poolclass=NullPool, pool_pre_ping=True,
        connect_args={"connect_timeout": 4},
    )


def _service_engine(user: str, password: str, port: int, role: str) -> Engine:
    engine = _connect("kpdesk", user, password, port)

    @event.listens_for(engine, "begin")
    def _set_limited_role(connection: Connection) -> None:
        connection.exec_driver_sql("SET LOCAL ROLE " + role)

    return engine


def _verify_connection(user: str, password: str, port: int, role: str) -> None:
    engine = _service_engine(user, password, port, role)
    try:
        with engine.begin() as conn:
            session_user, active_role, db = conn.execute(text("""
                SELECT session_user, current_user, current_database()
            """)).one()
            if (session_user, active_role, db) != (
                user, role, "kpdesk",
            ):
                raise RuntimeError("Runtime PostgreSQL session identity mismatch.")
            if conn.scalar(text("""
                SELECT has_table_privilege(current_user,'public.users','SELECT')
            """)) is not False:
                raise RuntimeError("Runtime can enumerate accounts.")
            if role == "kpdesk_auth_runtime":
                if conn.scalar(text("""
                    SELECT has_function_privilege(
                        current_user,'public.kpdesk_auth_credential(text,text)','EXECUTE')
                """)) is not True:
                    raise RuntimeError("Auth service cannot validate credentials.")
                if conn.scalar(text("""
                    SELECT has_table_privilege(current_user,
                                               'public.catalog_items','SELECT')
                """)) is not False:
                    raise RuntimeError("Auth service can read catalog products.")
            else:
                if conn.scalar(text("""
                    SELECT has_function_privilege(
                        current_user,'public.kpdesk_auth_credential(text,text)','EXECUTE')
                """)) is not False:
                    raise RuntimeError("Tenant service can read password material.")
                if conn.scalar(text("""
                    SELECT has_table_privilege(
                        current_user,'public.tenant_catalog_active','SELECT')
                """)) is not True:
                    raise RuntimeError("Tenant service cannot read scoped catalog.")
                if conn.scalar(text("""
                    SELECT public.kpdesk_session_tenant_allowed(
                        '00000000-0000-0000-0000-000000000000'::uuid,'read'
                    )
                """)) is not False:
                    raise RuntimeError("Tenant session validator did not deny missing token.")
    finally:
        engine.dispose()


def _lock_windows_secret_file(path: Path) -> None:
    if os.name != "nt":
        raise RuntimeError("This project requires Windows owner-only ACL setup.")
    identity = subprocess.run(
        ["whoami"], capture_output=True, text=True, check=True, timeout=10,
    ).stdout.strip()
    if not identity:
        raise RuntimeError("Cannot establish Windows login identity.")
    result = subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", identity + ":(F)"],
        capture_output=True, text=True, timeout=15, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("Unable to restrict Windows credentials-file ACL.")


def provision() -> None:
    values = dotenv_values(ROOT / ".env")
    owner_pass = values.get("KP_DESK_DB_PASSWORD")
    if not isinstance(owner_pass, str) or len(owner_pass) < 32:
        raise RuntimeError("Local PostgreSQL owner credential unavailable.")
    port = int(values.get("KP_DESK_DB_PORT") or 55432)
    owner = _connect("kpdesk", "kpdesk", owner_pass, port)
    generated = {user: secrets.token_urlsafe(36) for user, _ in TARGETS}
    if any(_PASSWORD.fullmatch(password) is None for password in generated.values()):
        raise RuntimeError("Unexpected generated credential format.")

    try:
        with owner.connect() as conn:
            if conn.scalar(text("SELECT version_num FROM alembic_version")) != (
                "0014_live_auth_rls"
            ):
                raise RuntimeError("Live RLS release is not applied.")
            for _, role in TARGETS:
                privileges = conn.execute(text("""
                    SELECT rolcanlogin, rolsuper, rolbypassrls
                    FROM pg_roles WHERE rolname=:name
                """), {"name": role}).one_or_none()
                if privileges != (False, False, False):
                    raise RuntimeError("Required NOLOGIN service role not ready.")
            existing = [
                bool(conn.scalar(text("""
                    SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:name)
                """), {"name": user}))
                for user, _ in TARGETS
            ]
        if any(existing):
            if not all(existing) or not SECRETS_FILE.is_file():
                raise RuntimeError(
                    "Partial existing service identities: manual review required."
                )
            saved = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
            for user, role in TARGETS:
                password = saved.get(user)
                if not isinstance(password, str):
                    raise TypeError("Existing service secret file inconsistent.")
                _verify_connection(user, password, port, role)
            print("LIVE_SERVICE_IDENTITIES_VERIFIED existing=true "
                  "separate_sessions=true production_schema=0014")
            return
        if SECRETS_FILE.exists():
            raise RuntimeError("Secret file exists without matching database roles.")
        tmp = SECRETS_FILE.with_suffix(".json.pending")
        if tmp.exists():
            raise RuntimeError("Existing incomplete credentials file.")
        SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        staged = False
        committed = False
        try:
            # Prepare and ACL-lock a private file BEFORE creating working roles.
            with tmp.open("x", encoding="utf-8") as handle:
                json.dump(generated, handle, indent=2)
                handle.write("\n")
            staged = True
            _lock_windows_secret_file(tmp)
            with owner.begin() as conn:
                conn.execute(text("SELECT pg_advisory_xact_lock(3219421601)"))
                for user, role in TARGETS:
                    if conn.scalar(text("""
                        SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:name)
                    """), {"name": user}):
                        raise RuntimeError("Concurrent service provisioning detected.")
                    # Generated token_urlsafe secrets are checked against a closed
                    # SQL-quote-free alphabet; role identifiers are constants.
                    conn.exec_driver_sql(
                        f"CREATE ROLE {user} LOGIN NOINHERIT NOSUPERUSER "
                        "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS "
                        f"PASSWORD '{generated[user]}'"
                    )
                    conn.exec_driver_sql(f"GRANT {role} TO {user}")
            committed = True
            for user, role in TARGETS:
                _verify_connection(user, generated[user], port, role)
            tmp.replace(SECRETS_FILE)
        except Exception:
            # A failed runtime provision must not leave usable credentials
            # or orphan LOGIN identities behind. This does not touch NOLOGIN roles.
            if committed:
                with owner.begin() as conn:
                    for user, role in reversed(TARGETS):
                        conn.exec_driver_sql(f"REVOKE {role} FROM {user}")
                        conn.exec_driver_sql(f"DROP ROLE {user}")
            raise
        finally:
            if staged and tmp.exists():
                tmp.unlink()
        print("LIVE_SERVICE_IDENTITIES_PROVISIONED "
              "service_logins=2 auth_tenant_separated=true "
              "windows_credentials_acl=owner_only routes_still_unmounted=true")
    finally:
        owner.dispose()


if __name__ == "__main__":
    provision()
