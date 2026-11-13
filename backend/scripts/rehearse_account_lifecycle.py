"""A15 end-to-end SQL lifecycle on disposable 0014 database, never on live users."""
from __future__ import annotations

import hashlib
import importlib
import secrets
import subprocess

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.pool import NullPool

from kpdesk.db.auth_sessions import hash_password
from scripts.rehearse_release_snapshot import ROOT, _docker_input

MIGRATION = importlib.import_module("migrations.versions.0015_verified_accounts")


def rehearse() -> None:
    values = dotenv_values(ROOT / ".env")
    password = values.get("KP_DESK_DB_PASSWORD")
    if not isinstance(password, str) or len(password) < 32:
        raise RuntimeError("Owner credentials not configured.")
    port = int(values.get("KP_DESK_DB_PORT") or 55432)

    def engine(database: str, *, admin: bool = False) -> Engine:
        return create_engine(
            URL.create(
                "postgresql+psycopg", username="kpdesk", password=password,
                host="127.0.0.1", port=port, database=database,
            ),
            isolation_level="AUTOCOMMIT" if admin else None,
            poolclass=NullPool,
        )

    nonce = secrets.token_hex(6)
    name = "kpdesk_accounts_e2e_" + nonce
    marker = "kpdesk:accounts-rehearsal:" + nonce
    dump = ROOT / "backups" / "2026-10-10-a15-rehearsal" / (nonce + ".dump")
    dump.parent.mkdir(parents=True, exist_ok=True)
    owner = engine("kpdesk")
    admin = engine("postgres", admin=True)
    clone = None
    created = False
    try:
        with owner.connect() as conn:
            if conn.scalar(text("SELECT version_num FROM alembic_version")) != (
                "0014_live_auth_rls"
            ):
                raise RuntimeError("A15 rehearsal requires 0014 on local DB.")
            if conn.scalar(text("SELECT count(*) FROM users")):
                raise RuntimeError("Real users exist: unreviewed account migration.")
        with dump.open("xb") as out:
            subprocess.run(
                ["docker", "exec", "kpdesk-db-1", "pg_dump", "-U", "kpdesk",
                 "-d", "kpdesk", "-Fc", "--no-owner", "--no-acl",
                 "--serializable-deferrable"],
                stdout=out, stderr=subprocess.PIPE, check=True, timeout=120,
            )
        with admin.connect() as conn:
            if conn.scalar(text("""
                SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=:name)
            """), {"name": name}):
                raise RuntimeError("Refusing reuse of disposable DB.")
            conn.exec_driver_sql(f"CREATE DATABASE {name} TEMPLATE template0")
            created = True
            conn.exec_driver_sql(f"COMMENT ON DATABASE {name} IS '{marker}'")
        _docker_input(
            "pg_restore", "-U", "kpdesk", "--dbname=" + name,
            "--single-transaction", "--exit-on-error",
            "--no-owner", "--no-acl", input_file=dump,
        )
        clone = engine(name)
        with clone.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                MIGRATION.upgrade()
            for table in ("account_email_challenges", "account_email_outbox",
                          "account_action_limits"):
                assert conn.scalar(text("SELECT to_regclass(:name) IS NOT NULL"),
                                   {"name": "public." + table})
            uid = hashlib.sha256(b"signup-e2e").hexdigest()
            first = secrets.token_urlsafe(32)
            first_hash = hashlib.sha256(first.encode()).hexdigest()
            later = secrets.token_urlsafe(32)
            later_hash = hashlib.sha256(later.encode()).hexdigest()
            pw_hash = hash_password("A15InitialTestPassword!2026")
            conn.exec_driver_sql("SET LOCAL ROLE kpdesk_auth_runtime")
            inserted = conn.scalar(text("""
                SELECT public.kpdesk_account_signup(
                    :email,:pw,:org,:sha,:cipher,:peer)
            """), {
                "email": "a15@example.test", "pw": pw_hash,
                "org": "A15 real company",
                "sha": first_hash, "cipher": "g" * 120, "peer": "127.0.0.1",
            })
            if inserted is not True:
                raise RuntimeError("Signup SQL gateway did not create account.")
            if conn.scalar(text("""
                SELECT public.kpdesk_account_signup(
                    :email,:pw,:org,:sha,:cipher,:peer)
            """), {
                "email": "a15@example.test", "pw": pw_hash,
                "org": "Duplicate Org",
                "sha": uid, "cipher": "f" * 120, "peer": "127.0.0.1",
            }) is not False:
                raise RuntimeError("Duplicate email account was created.")
            claimed = conn.execute(text("""
                SELECT challenge_id, recipient_email, purpose, token_ciphertext
                FROM public.kpdesk_email_claim()
            """)).one_or_none()
            if claimed is None or claimed.purpose != "signup":
                raise RuntimeError("Transactional signup outbox was not delivered.")
            if not conn.scalar(text("""
                SELECT public.kpdesk_email_complete(:id,:provider,true)
            """), {"id": claimed.challenge_id, "provider": "test-mail-id"}):
                raise RuntimeError("Outbox acknowledgment was rejected.")
            verified = conn.execute(text("""
                SELECT identity_id,organization_id
                FROM public.kpdesk_account_verify(:sha)
            """), {"sha": first_hash}).one_or_none()
            if verified is None:
                raise RuntimeError("Cannot confirm user and create organization.")
            if conn.execute(text("""
                SELECT * FROM public.kpdesk_account_verify(:sha)
            """), {"sha": first_hash}).one_or_none() is not None:
                raise RuntimeError("Consumed signup token was replayed.")
            if conn.scalar(text("""
                SELECT public.kpdesk_account_reset_request(
                    :email,:sha,:cipher,:peer)
            """), {"email": "a15@example.test", "sha": later_hash,
                   "cipher": "h"*120, "peer": "127.0.0.1"}) is not True:
                raise RuntimeError("Reset token not created.")
            if conn.scalar(text("""
                SELECT public.kpdesk_account_reset_consume(:sha,:pw)
            """), {"sha": later_hash,
                   "pw": hash_password("A15UpdatedPassword!2026")}) is not True:
                raise RuntimeError("Password reset not consumed.")
            if conn.scalar(text("""
                SELECT public.kpdesk_account_reset_consume(:sha,:pw)
            """), {"sha": later_hash, "pw": pw_hash}) is not False:
                raise RuntimeError("Password reset token was replayed.")
            conn.exec_driver_sql("SET LOCAL ROLE kpdesk")
            if conn.scalar(text("SELECT count(*) FROM users")) != 1:
                raise RuntimeError("User provisioning count mismatch.")
            if conn.scalar(text("SELECT count(*) FROM organizations")) != 1:
                raise RuntimeError("Organization provisioning count mismatch.")
            if conn.scalar(text("""
                SELECT count(*) FROM organization_memberships
                WHERE role='owner' AND status='active'
            """)) != 1:
                raise RuntimeError("Owner membership not established.")
            # All SQL DML above is isolated in this disposable database.
        print("A15_ACCOUNT_LIFECYCLE_SQL_PASSED "
              "registration=true duplicate_denied=true "
              "encrypted_outbox_claim=true email_verified=true "
              "owner_organization_created=true token_replay_denied=true "
              "password_reset=true production_users_unchanged=true")
    finally:
        if clone is not None:
            clone.dispose()
        try:
            if created:
                with admin.connect() as conn:
                    actual = conn.scalar(text("""
                        SELECT shobj_description(oid,'pg_database')
                        FROM pg_database WHERE datname=:name
                    """), {"name": name})
                    if actual != marker:
                        raise RuntimeError("Refusing cleanup of unknown database.")
                    conn.exec_driver_sql(f"DROP DATABASE {name} WITH (FORCE)")
        finally:
            admin.dispose()
            owner.dispose()
            if dump.exists():
                dump.unlink()


if __name__ == "__main__":
    rehearse()
