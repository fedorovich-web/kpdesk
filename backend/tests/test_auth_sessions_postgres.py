"""Real PostgreSQL A5 server sessions exercised transactionally under app role.

A3/A4/A5 DDL, synthetic accounts, password hashes and grants are rolled back.
No public login endpoints, cookies or production database modifications.
"""
from __future__ import annotations

import importlib
import uuid
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError

from kpdesk.db.auth_sessions import (
    SESSION_LIFETIME,
    AuthSessionRepository,
    LoginDenied,
    SessionDenied,
    csrf_for_session,
    hash_password,
    verify_password,
)
from kpdesk.db.tenant_access import TenantAccessDenied, authorize_tenant_transaction

ROOT = Path(__file__).resolve().parents[2]


def test_argon2id_password_is_salted_and_plaintext_never_stored() -> None:
    password = "An-Actual-Secret-Passphrase-2026"
    first = hash_password(password)
    second = hash_password(password)
    assert first.startswith("$argon2id$")
    assert second.startswith("$argon2id$")
    assert first != second
    assert password not in first
    assert verify_password(first, password)
    assert verify_password(second, password)
    assert not verify_password(first, password + "-wrong")
    assert not verify_password(None, password)
    assert not verify_password("corrupt hash", password)
    with pytest.raises(ValueError):
        hash_password("short")


def test_tenant_sessions_login_csrf_revoke_membership_and_transactional_rollback() -> None:
    env = dotenv_values(ROOT / ".env")
    password = env.get("KP_DESK_DB_PASSWORD")
    assert isinstance(password, str) and len(password) >= 32
    engine = create_engine(URL.create(
        "postgresql+psycopg", username="kpdesk", password=password,
        host="127.0.0.1", port=int(env.get("KP_DESK_DB_PORT") or 55432),
        database="kpdesk_tenant_test",
    ))
    user, unrelated, organization, other_org = (uuid.uuid4() for _ in range(4))
    actual_password = "Safe-Locally-Tested-Session-Passphrase"
    session_hash: str | None = None
    try:
        with engine.connect() as conn:
            transaction = conn.begin()
            try:
                assert conn.scalar(text("SELECT current_database()")) == (
                    "kpdesk_tenant_test"
                )
                assert conn.scalar(text(
                    "SELECT version_num FROM alembic_version"
                )) == "0006_legacy_quarantine"
                with Operations.context(MigrationContext.configure(conn)):
                    for name in (
                        "0007_tenant_contract_rls",
                        "0008_supplier_review_rls",
                        "0009_auth_sessions",
                        "0010_auth_gateway",
                    ):
                        importlib.import_module("migrations.versions." + name).upgrade()
                assert conn.scalar(text(
                    "SELECT to_regclass('public.auth_sessions') IS NOT NULL"
                )) is True
                for org, label in ((organization, "Org A"), (other_org, "Org B")):
                    conn.execute(text("""
                        INSERT INTO organizations (id,name,status)
                        VALUES (:org,:label,'active')
                    """), {"org": org, "label": label})
                conn.execute(text("""
                    INSERT INTO users
                        (id,email,password_hash,status,email_verified_at)
                    VALUES
                        (:id,'alice@a5.local',:password,'active',now()),
                        (:other,'not-verified@a5.local',:password,'active',NULL)
                """), {
                    "id": user, "other": unrelated,
                    "password": hash_password(actual_password),
                })
                conn.execute(text("""
                    INSERT INTO organization_memberships
                        (organization_id,user_id,role,status)
                    VALUES (:org,:user,'owner','active')
                """), {"org": organization, "user": user})
                conn.execute(text("GRANT USAGE ON SCHEMA public TO kpdesk_app"))
                for table in ("users", "organizations", "organization_memberships", "auth_sessions"):
                    assert conn.scalar(text("""
                        SELECT has_table_privilege('kpdesk_app', :table, 'SELECT')
                    """), {"table": "public." + table}) is False
                assert conn.scalar(text("""
                    SELECT has_table_privilege('kpdesk_app',
                        'public.auth_login_limits','SELECT')
                """)) is False
                conn.execute(text("SET ROLE kpdesk_app"))
                for inaccessible in (
                    "SELECT password_hash FROM public.users LIMIT 1",
                    "SELECT role FROM public.organization_memberships LIMIT 1",
                    "SELECT token_sha256 FROM public.auth_sessions LIMIT 1",
                    "SELECT bucket FROM public.auth_login_limits LIMIT 1",
                ):
                    with pytest.raises(DBAPIError), conn.begin_nested():
                        conn.execute(text(inaccessible))
                service = AuthSessionRepository(conn)
                for email, proposed_password in (
                    ("no-one@a5.local", actual_password),
                    ("not-verified@a5.local", actual_password),
                    ("alice@a5.local", "wrong password"),
                ):
                    with pytest.raises(LoginDenied, match="Invalid login"):
                        service.login(email, proposed_password, peer="192.0.2.19")
                issued = service.login(" ALICE@A5.LOCAL ", actual_password, peer="192.0.2.19")
                assert issued.user_id == user
                assert issued.session_token != issued.csrf_token
                assert csrf_for_session(issued.session_token) == issued.csrf_token
                assert SESSION_LIFETIME.days == 7
                assert len(issued.session_token) == len(issued.csrf_token) == 43
                assert issued.expires_at.tzinfo is not None
                session_hash = __import__("hashlib").sha256(
                    issued.session_token.encode("ascii")
                ).hexdigest()
                conn.execute(text("RESET ROLE"))
                record = conn.execute(text("""
                    SELECT token_sha256,csrf_sha256
                    FROM auth_sessions WHERE token_sha256 = :sha
                """), {"sha": session_hash}).one()
                assert record.token_sha256 == session_hash
                assert issued.session_token not in str(record)
                assert issued.csrf_token not in str(record)
                conn.execute(text("SET ROLE kpdesk_app"))
                identity = service.authenticate(issued.session_token)
                assert identity.user_id == user
                with pytest.raises(SessionDenied):
                    service.authenticate(issued.session_token, operation="write")
                with pytest.raises(SessionDenied):
                    service.authenticate(
                        issued.session_token, operation="write",
                        csrf=issued.session_token,
                    )
                assert service.authenticate(
                    issued.session_token, operation="write", csrf=issued.csrf_token,
                ) == identity
                assert authorize_tenant_transaction(
                    conn, identity, organization, "manage",
                ).organization_id == organization
                with pytest.raises(TenantAccessDenied):
                    authorize_tenant_transaction(
                        conn, identity, other_org, "read",
                    )
                # Membership revoked: session remains valid for login, but
                # cannot access any tenant data after re-validation.
                conn.execute(text("RESET ROLE"))
                conn.execute(text("""
                    UPDATE organization_memberships
                    SET status='revoked' WHERE organization_id=:org AND user_id=:user
                """), {"org": organization, "user": user})
                conn.execute(text("SET ROLE kpdesk_app"))
                assert service.authenticate(issued.session_token).user_id == user
                with pytest.raises(TenantAccessDenied):
                    authorize_tenant_transaction(conn, identity, organization, "read")
                assert service.revoke(issued.session_token) is True
                assert service.revoke(issued.session_token) is False
                with pytest.raises(SessionDenied):
                    service.authenticate(issued.session_token)
                with pytest.raises(SessionDenied):
                    service.authenticate("not-a-token")
                # Expiration and disabled-account status are verified per request.
                expired = service.login("alice@a5.local", actual_password, peer="192.0.2.19")
                conn.execute(text("RESET ROLE"))
                conn.execute(text("""
                    UPDATE auth_sessions
                    SET created_at = now() - interval '2 days',
                        expires_at = now() - interval '1 day'
                    WHERE token_sha256 = :hash
                """), {
                    "hash": __import__("hashlib").sha256(
                        expired.session_token.encode("ascii")
                    ).hexdigest(),
                })
                conn.execute(text("SET ROLE kpdesk_app"))
                with pytest.raises(SessionDenied):
                    service.authenticate(expired.session_token)
                current = service.login("alice@a5.local", actual_password, peer="192.0.2.19")
                conn.execute(text("RESET ROLE"))
                conn.execute(text("""
                    UPDATE users SET status='disabled' WHERE id=:user
                """), {"user": user})
                conn.execute(text("SET ROLE kpdesk_app"))
                with pytest.raises(SessionDenied):
                    service.authenticate(current.session_token)
                with pytest.raises(LoginDenied, match="Invalid login"):
                    service.login("alice@a5.local", actual_password, peer="192.0.2.19")
                conn.execute(text("RESET ROLE"))
            finally:
                transaction.rollback()
        with engine.connect() as verify:
            assert verify.scalar(text(
                "SELECT version_num FROM alembic_version"
            )) == "0006_legacy_quarantine"
            assert verify.scalar(text(
                "SELECT to_regclass('public.auth_sessions')"
            )) is None
            assert verify.scalar(text(
                "SELECT to_regclass('public.supplier_reviewed_profiles')"
            )) is None
            assert session_hash is not None
            assert verify.scalar(text(
                "SELECT count(*) FROM users WHERE id=:id"
            ), {"id": user}) == 0
    finally:
        engine.dispose()


def test_session_migration_refuses_production_readonly() -> None:
    env = dotenv_values(ROOT / ".env")
    password = env.get("KP_DESK_DB_PASSWORD")
    assert isinstance(password, str) and len(password) >= 32
    engine = create_engine(URL.create(
        "postgresql+psycopg", username="kpdesk", password=password,
        host="127.0.0.1", port=int(env.get("KP_DESK_DB_PORT") or 55432),
        database="kpdesk",
    ), connect_args={"options": "-c default_transaction_read_only=on"})
    try:
        with engine.connect() as conn:
            with (
                Operations.context(MigrationContext.configure(conn)),
                pytest.raises(RuntimeError, match="forbidden outside kpdesk_tenant_test"),
            ):
                importlib.import_module(
                    "migrations.versions.0009_auth_sessions"
                ).upgrade()
            assert conn.scalar(text(
                "SELECT version_num FROM alembic_version"
            )) == "0031_catalog_taxonomy_v4"
    finally:
        engine.dispose()
