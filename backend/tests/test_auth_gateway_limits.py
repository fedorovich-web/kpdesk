"""Transaction-only rate-limit contract for restricted PostgreSQL auth gateway."""
from __future__ import annotations

import importlib
from pathlib import Path

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

ROOT = Path(__file__).resolve().parents[2]


def test_postgres_shared_login_limits_are_account_and_peer_scoped() -> None:
    creds = dotenv_values(ROOT / ".env")
    password = creds.get("KP_DESK_DB_PASSWORD")
    assert isinstance(password, str) and len(password) >= 32
    engine = create_engine(URL.create(
        "postgresql+psycopg", username="kpdesk", password=password,
        host="127.0.0.1", port=int(creds.get("KP_DESK_DB_PORT") or 55432),
        database="kpdesk_tenant_test",
    ))
    statement = text("""
        SELECT identity_id,secret_hash,usable,limited
        FROM public.kpdesk_auth_credential(:email,:peer)
    """)
    try:
        with engine.connect() as conn:
            transaction = conn.begin()
            try:
                assert conn.scalar(text("SELECT current_database()")) == "kpdesk_tenant_test"
                assert conn.scalar(text(
                    "SELECT version_num FROM alembic_version"
                )) == "0006_legacy_quarantine"
                with Operations.context(MigrationContext.configure(conn)):
                    for version in (
                        "0007_tenant_contract_rls", "0008_supplier_review_rls",
                        "0009_auth_sessions", "0010_auth_gateway",
                    ):
                        importlib.import_module("migrations.versions." + version).upgrade()
                assert conn.scalar(text("""
                    SELECT has_table_privilege('kpdesk_app','public.users','SELECT')
                """)) is False
                assert conn.scalar(text("""
                    SELECT has_table_privilege(
                        'kpdesk_app','public.auth_login_limits','SELECT'
                    )
                """)) is False
                conn.execute(text("SET ROLE kpdesk_app"))
                assert conn.scalar(text("SELECT current_user")) == "kpdesk_app"
                for attempt in range(6):
                    result = conn.execute(statement, {
                        "email": "first@limits.invalid",
                        "peer": "192.0.2.51",
                    }).one_or_none()
                    assert result is None, attempt
                account_limited = conn.execute(statement, {
                    "email": "first@limits.invalid",
                    "peer": "198.51.100.51",
                }).one()
                assert account_limited.limited is True
                assert account_limited.secret_hash is None
                assert account_limited.identity_id is None
                # Many different users from one peer share the global IP bucket.
                for index in range(30):
                    result = conn.execute(statement, {
                        "email": f"unique-{index}@limits.invalid",
                        "peer": "203.0.113.9",
                    }).one_or_none()
                    assert result is None, index
                blocked_peer = conn.execute(statement, {
                    "email": "yet-another@limits.invalid",
                    "peer": "203.0.113.9",
                }).one()
                assert blocked_peer.limited is True

                conn.execute(text("RESET ROLE"))
                # Simulate clock passage only through the privileged test owner,
                # never by granting the application UPDATE to throttle buckets.
                conn.execute(text("""
                    UPDATE auth_login_limits
                    SET window_start=clock_timestamp() - interval '16 minutes'
                """))
                conn.execute(text("SET ROLE kpdesk_app"))
                expired_window = conn.execute(statement, {
                    "email": "first@limits.invalid",
                    "peer": "192.0.2.51",
                }).one_or_none()
                assert expired_window is None
                conn.execute(text("RESET ROLE"))
            finally:
                transaction.rollback()
        with engine.connect() as check:
            assert check.scalar(text(
                "SELECT version_num FROM alembic_version"
            )) == "0006_legacy_quarantine"
            assert check.scalar(text(
                "SELECT to_regclass('public.auth_login_limits')"
            )) is None
            assert check.scalar(text(
                "SELECT to_regprocedure('public.kpdesk_auth_credential(text,text)')"
            )) is None
    finally:
        engine.dispose()
