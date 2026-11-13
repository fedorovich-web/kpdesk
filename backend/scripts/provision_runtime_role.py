"""Provision only KPDesk's least-privileged runtime account.

Run without --apply for inspection. Requires deliberate --apply for DDL.
Never pass credentials on command line, print them, or use the owner for app runtime.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import psycopg
from dotenv import dotenv_values
from psycopg import sql

ROOT = Path(__file__).resolve().parents[2]
ROLE = "kpdesk_app"
DATABASE = "kpdesk"
TABLE_ACCESS = {
    "catalog_versions": ("SELECT", "INSERT"),
    "catalog_items": ("SELECT", "INSERT"),
    "catalog_active": ("SELECT", "UPDATE"),
    "idempotency_records": ("SELECT", "INSERT"),
    "quote_documents": ("SELECT", "INSERT", "UPDATE"),
    "quote_revisions": ("SELECT", "INSERT"),
    "quote_write_requests": ("SELECT", "INSERT"),
}

def run(apply: bool) -> None:
    env = dotenv_values(ROOT / ".env")
    owner_password = env.get("KP_DESK_DB_PASSWORD")
    runtime_password = env.get("KP_DESK_APP_PASSWORD")
    if not isinstance(owner_password, str) or len(owner_password) < 32:
        raise RuntimeError("Admin DB password is not configured.")
    if not isinstance(runtime_password, str) or len(runtime_password) < 32:
        raise RuntimeError("App DB password is not configured.")
    with psycopg.connect(
        dbname=DATABASE, user="kpdesk", password=owner_password,
        host="127.0.0.1", port=int(env.get("KP_DESK_DB_PORT") or "55432"),
        connect_timeout=3,
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_user")
            if cursor.fetchone() != (DATABASE, "kpdesk"):
                raise RuntimeError("Refusing unexpected database/administrator.")
            cursor.execute(
                "SELECT tablename, tableowner FROM pg_tables "
                "WHERE schemaname='public' AND tablename=ANY(%s)",
                (list(TABLE_ACCESS),),
            )
            found: dict[str, str] = dict(cursor.fetchall())
            if found != {name: "kpdesk" for name in TABLE_ACCESS}:
                raise RuntimeError("App tables missing or not owned by expected migration role.")
            cursor.execute("SELECT version_num FROM alembic_version")
            if cursor.fetchall() != [("0005_tenant_expand",)]:
                raise RuntimeError("Unexpected Alembic schema version.")
            cursor.execute(
                "SELECT rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,"
                "rolreplication,rolbypassrls FROM pg_roles WHERE rolname=%s", (ROLE,),
            )
            role = cursor.fetchone()
            if role is not None and role != (True, False, False, False, False, False):
                raise RuntimeError("Refusing to modify existing role with unexpected attributes.")
            cursor.execute(
                "SELECT count(*) FROM pg_auth_members am JOIN pg_roles r "
                "ON r.oid=am.member WHERE r.rolname=%s", (ROLE,),
            )
            membership_row = cursor.fetchone()
            if membership_row is None or membership_row[0] != 0:
                raise RuntimeError("Refusing unexpected runtime role memberships.")
            print("SCHEMA_OK=true MIGRATION=0005_tenant_expand "
                  "APP_ROLE=" + ("present" if role else "missing"))
            if not apply:
                print("DRY_RUN: no changes; --apply required to provision role and grants.")
                return
            if role is None:
                cursor.execute(sql.SQL(
                    "CREATE ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}"
                ).format(sql.Identifier(ROLE), sql.Literal(runtime_password)))
                print("CREATED runtime role")
            else:
                cursor.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier(ROLE), sql.Literal(runtime_password)
                ))
                print("SYNCHRONIZED runtime password")
            cursor.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(DATABASE), sql.Identifier(ROLE)
            ))
            cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                sql.Identifier(ROLE)
            ))
            for table, grants in TABLE_ACCESS.items():
                permissions = sql.SQL(", ").join(sql.SQL(permission) for permission in grants)
                cursor.execute(sql.SQL("GRANT {} ON TABLE public.{} TO {}").format(
                    permissions, sql.Identifier(table), sql.Identifier(ROLE)
                ))
                print("GRANTED", table, ",".join(grants))
            # DDL and grants are committed together by the context manager.
        print("PROVISIONING_COMPLETE")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="Create/repair kpdesk_app and grant only explicit table permissions")
    run(parser.parse_args().apply)
