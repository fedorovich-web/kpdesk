"""A7 test-only PostgreSQL isolation: app cannot create/revoke sessions.

Migration DDL, role creation, grants, credentials and synthetic users roll back
together on kpdesk_tenant_test. This does NOT create real deployed auth service.
"""
from __future__ import annotations

import hashlib
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

from kpdesk.db.auth_sessions import AuthSessionRepository, hash_password


def test_a7_auth_role_split_and_impossible_app_session_mint() -> None:
    credentials = dotenv_values(Path(__file__).resolve().parents[2] / ".env")
    password = credentials.get("KP_DESK_DB_PASSWORD")
    assert isinstance(password, str) and len(password) >= 32
    engine = create_engine(URL.create(
        "postgresql+psycopg", username="kpdesk", password=password,
        host="127.0.0.1", port=int(credentials.get("KP_DESK_DB_PORT") or 55432),
        database="kpdesk_tenant_test",
    ))
    alice = uuid.uuid4()
    password_alice = "Correct-Horse-Password-For-Test"
    token_hash: str | None = None
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                assert connection.scalar(text(
                    "SELECT version_num FROM alembic_version"
                )) == "0006_legacy_quarantine"
                with Operations.context(MigrationContext.configure(connection)):
                    for version in (
                        "0007_tenant_contract_rls",
                        "0008_supplier_review_rls",
                        "0009_auth_sessions",
                        "0010_auth_gateway",
                        "0011_auth_role_split",
                    ):
                        importlib.import_module(
                            "migrations.versions." + version
                        ).upgrade()
                connection.execute(text("""
                    INSERT INTO users(id,email,password_hash,status,email_verified_at)
                    VALUES (:id,'alice@split.invalid',:hash,'active',clock_timestamp())
                """), {
                    "id": alice, "hash": hash_password(password_alice),
                })
                for role in ("kpdesk_app", "kpdesk_auth_test"):
                    assert connection.scalar(text("""
                        SELECT rolsuper OR rolbypassrls
                        FROM pg_roles WHERE rolname=:role
                    """), {"role": role}) is False
                    for table in ("users", "auth_sessions", "auth_login_limits"):
                        assert connection.scalar(text("""
                            SELECT has_table_privilege(
                                :role, :name, 'SELECT'
                            )
                        """), {"role": role, "name": "public." + table}) is False
                assert connection.scalar(text("""
                    SELECT rolcanlogin FROM pg_roles
                    WHERE rolname='kpdesk_auth_test'
                """)) is False
                assert connection.scalar(text("""
                    SELECT pg_has_role('kpdesk_app','kpdesk_auth_test','MEMBER')
                """)) is False
                assert connection.scalar(text("""
                    SELECT has_table_privilege(
                        'kpdesk_auth_test','public.catalog_versions','SELECT'
                    )
                """)) is False
                for operation, allowed in (
                    ("kpdesk_auth_credential(text,text)", False),
                    ("kpdesk_auth_revoke(text)", False),
                    ("kpdesk_auth_session(text)", True),
                    ("kpdesk_auth_member(uuid,uuid)", True),
                ):
                    assert connection.scalar(text("""
                        SELECT has_function_privilege(:role, :signature, 'EXECUTE')
                    """), {
                        "role": "kpdesk_app",
                        "signature": "public." + operation,
                    }) is allowed
                assert connection.scalar(text("""
                    SELECT has_table_privilege(
                        'kpdesk_app','public.auth_sessions','INSERT'
                    )
                """)) is False
                assert connection.scalar(text("""
                    SELECT has_table_privilege(
                        'kpdesk_auth_test','public.auth_sessions','INSERT'
                    )
                """)) is True

                connection.execute(text("SET ROLE kpdesk_app"))
                app_reader = AuthSessionRepository(connection)
                with pytest.raises(RuntimeError, match="not allowed"):
                    app_reader.login(
                        "alice@split.invalid", password_alice, peer="192.0.2.1",
                    )
                with pytest.raises(DBAPIError), connection.begin_nested():
                    connection.execute(text("""
                        INSERT INTO auth_sessions(
                            token_sha256,user_id,csrf_sha256,expires_at
                        ) VALUES (
                            :token,:identity,:csrf,clock_timestamp()+interval '1 hour'
                        )
                    """), {
                        "token": "a" * 64, "identity": alice, "csrf": "b" * 64,
                    })
                with pytest.raises(DBAPIError), connection.begin_nested():
                    connection.execute(text("""
                        SELECT * FROM public.kpdesk_auth_credential(
                            'alice@split.invalid','192.0.2.1'
                        )
                    """))
                with pytest.raises(DBAPIError), connection.begin_nested():
                    connection.execute(text("""
                        SELECT public.kpdesk_auth_revoke(:hash)
                    """), {"hash": "a" * 64})

                connection.execute(text("SET ROLE kpdesk_auth_test"))
                auth_service = AuthSessionRepository(connection)
                issued = auth_service.login(
                    "alice@split.invalid", password_alice, peer="192.0.2.1",
                )
                token_hash = hashlib.sha256(
                    issued.session_token.encode("ascii")
                ).hexdigest()
                connection.execute(text("SET ROLE kpdesk_app"))
                identity = app_reader.authenticate(issued.session_token)
                assert identity.user_id == alice
                assert app_reader.authenticate(
                    issued.session_token, operation="write", csrf=issued.csrf_token,
                ).user_id == alice
                with pytest.raises(RuntimeError, match="not allowed"):
                    app_reader.revoke(issued.session_token)
                connection.execute(text("SET ROLE kpdesk_auth_test"))
                assert auth_service.revoke(issued.session_token) is True
                connection.execute(text("SET ROLE kpdesk_app"))
                from kpdesk.db.auth_sessions import SessionDenied

                with pytest.raises(SessionDenied):
                    app_reader.authenticate(issued.session_token)
                connection.execute(text("RESET ROLE"))
            finally:
                transaction.rollback()
        with engine.connect() as verify:
            assert verify.scalar(text(
                "SELECT version_num FROM alembic_version"
            )) == "0006_legacy_quarantine"
            assert verify.scalar(text("""
                SELECT EXISTS(
                    SELECT 1 FROM pg_roles WHERE rolname='kpdesk_auth_test'
                )
            """)) is False
            assert verify.scalar(text(
                "SELECT to_regclass('public.auth_login_limits')"
            )) is None
            assert token_hash is not None
            assert verify.scalar(text(
                "SELECT count(*) FROM users WHERE id=:id"
            ), {"id": alice}) == 0
    finally:
        engine.dispose()
