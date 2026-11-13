"""Production read-only acceptance contract for the actual A13 live expand."""
from __future__ import annotations

from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool

TABLES = (
    "tenant_catalog_active", "tenant_catalog_idempotency",
    "tenant_quote_write_requests",
)


def test_real_0013_expansion_preserves_legacy_api_and_scoped_constraints() -> None:
    env = dotenv_values(Path(__file__).resolve().parents[2] / ".env")
    password = env.get("KP_DESK_DB_PASSWORD")
    assert isinstance(password, str) and len(password) >= 32
    engine = create_engine(
        URL.create(
            "postgresql+psycopg", username="kpdesk", password=password,
            host="127.0.0.1", port=int(env.get("KP_DESK_DB_PORT") or 55432),
            database="kpdesk",
        ),
        poolclass=NullPool,
        connect_args={"options": "-c default_transaction_read_only=on"},
    )
    try:
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT current_database()")) == "kpdesk"
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0031_catalog_taxonomy_v4"
            )
            assert conn.scalar(text("""
                SELECT tgenabled FROM pg_trigger
                WHERE tgrelid='public.quote_revisions'::regclass
                  AND tgname='quote_revision_no_update_delete'
            """)) == "O"
            assert conn.scalar(text("SELECT count(*) FROM catalog_active WHERE id=1")) == 1
            columns = {col["name"] for col in inspect(conn).get_columns("catalog_active")}
            assert {"id", "catalog_version_id", "organization_id"} <= columns
            inspector = inspect(conn)
            assert set(TABLES).issubset(inspector.get_table_names())
            for table in TABLES:
                assert conn.scalar(text(f"SELECT count(*) FROM {table}")) == 0
                assert conn.scalar(text("""
                    SELECT has_table_privilege('kpdesk_app', :table, 'SELECT')
                """), {"table": "public." + table}) is False
            for table, expected in (
                ("tenant_catalog_active", "fk_live_active_scoped_catalog"),
                ("tenant_catalog_idempotency", "fk_live_idempotency_scoped_catalog"),
                ("tenant_quote_write_requests", "fk_live_idempotency_scoped_revision"),
            ):
                names = {
                    row["name"] for row in inspector.get_foreign_keys(table)
                }
                assert expected in names
            assert conn.scalar(text("""
                SELECT count(*) FROM catalog_versions
            """)) == 0
    finally:
        engine.dispose()

def test_legacy_application_routes_remain_available_after_expand() -> None:
    from fastapi.testclient import TestClient

    from kpdesk.main import create_app

    with TestClient(create_app(testing=True)) as client:
        assert client.get("/api/health/live").status_code == 200
        assert client.get("/api/health/ready").status_code == 200
        # Public session activation remains a separate, intentional release step.
        assert client.get("/api/session/me").status_code == 404
