"""A3 migration exercised transactionally on kpdesk_tenant_test, then rolled back.

No production DDL, permanent test DDL, automated downgrade or role changes.
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
from sqlalchemy.exc import IntegrityError

DOMAIN_TABLES = (
    "catalog_versions", "catalog_items", "catalog_active",
    "idempotency_records", "quote_documents", "quote_revisions",
    "quote_write_requests",
)


def test_a3_contract_rls_isolated_ddl_rollback() -> None:
    settings = dotenv_values(Path(__file__).resolve().parents[2] / ".env")
    secret = settings.get("KP_DESK_DB_PASSWORD")
    assert isinstance(secret, str) and len(secret) >= 32
    engine = create_engine(URL.create(
        "postgresql+psycopg", username="kpdesk", password=secret,
        host="127.0.0.1", port=int(settings.get("KP_DESK_DB_PORT") or 55432),
        database="kpdesk_tenant_test",
    ))
    migration = importlib.import_module("migrations.versions.0007_tenant_contract_rls")
    organization_a = uuid.uuid4()
    organization_b = uuid.uuid4()
    version_a = uuid.uuid4()
    version_b = uuid.uuid4()
    quote_a = uuid.uuid4()
    quote_b = uuid.uuid4()
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                assert connection.scalar(text("select current_database()")) == "kpdesk_tenant_test"
                assert connection.scalar(text("select version_num from alembic_version")) == (
                    "0006_legacy_quarantine"
                )
                with Operations.context(MigrationContext.configure(connection)):
                    migration.upgrade()
                # Test-only, rolled back with the entire transaction.
                for org, label in ((organization_a, "A"), (organization_b, "B")):
                    connection.execute(
                        text("insert into organizations(id,name,status) values (:id,:name,'active')"),
                        {"id": org, "name": "A3 organization " + label},
                    )
                for org, version in ((organization_a, version_a), (organization_b, version_b)):
                    connection.execute(text("""
                        insert into catalog_versions (
                            id, source_sha256, original_filename, rows_count, organization_id
                        ) values (:id, :sha, 'a3-test-only', 1, :org)
                    """), {"id": version, "sha": "a" * 64, "org": org})
                for org, quote in ((organization_a, quote_a), (organization_b, quote_b)):
                    connection.execute(text("""
                        insert into quote_documents (id, current_revision, organization_id)
                        values (:id, 1, :org)
                    """), {"id": quote, "org": org})
                for org, version, quote in (
                    (organization_a, version_a, quote_a),
                    (organization_b, version_b, quote_b),
                ):
                    connection.execute(text("""
                        insert into quote_revisions (
                            quote_id, number, catalog_version_id, snapshot,
                            snapshot_sha256, is_complete, total_rub, organization_id
                        ) values (:quote, 1, :version, '{}'::jsonb, :hash, false, null, :org)
                    """), {"quote": quote, "version": version, "hash": "a" * 64, "org": org})


                assert connection.scalar(text("select version_num from alembic_version")) == (
                    "0006_legacy_quarantine"
                )  # Direct transactional DDL test, NOT an Alembic upgrade command.
                for table in DOMAIN_TABLES:
                    nullable = connection.scalar(text("""
                        select is_nullable from information_schema.columns
                        where table_schema='public' and table_name=:table
                        and column_name='organization_id'
                    """), {"table": table})
                    assert nullable == "NO", table
                    rls = connection.execute(text("""
                        select relrowsecurity, relforcerowsecurity
                        from pg_class where oid = to_regclass(:table)
                    """), {"table": table}).one()
                    assert rls == (True, True), table

                connection.execute(text("grant usage on schema public to kpdesk_app"))
                for table in DOMAIN_TABLES:
                    connection.exec_driver_sql(
                        f"GRANT SELECT, INSERT ON TABLE public.{table} TO kpdesk_app"
                    )
                connection.execute(text("set role kpdesk_app"))
                assert connection.scalar(text("select current_user")) == "kpdesk_app"
                assert connection.scalar(text("select count(*) from catalog_versions")) == 0
                connection.execute(text(
                    "select set_config('kpdesk.organization_id', :org, true)"
                ), {"org": str(organization_a)})
                assert connection.scalar(text("""
                    select count(*) from catalog_versions where id=:id
                """), {"id": version_a}) == 1
                assert connection.scalar(text("""
                    select count(*) from catalog_versions where id=:id
                """), {"id": version_b}) == 0

                with pytest.raises(IntegrityError), connection.begin_nested():
                    connection.execute(text("""
                        insert into catalog_active (organization_id, catalog_version_id)
                        values (:org, :other)
                    """), {"org": organization_a, "other": version_b})

                with pytest.raises(IntegrityError), connection.begin_nested():
                    connection.execute(text("""
                        insert into quote_write_requests (
                            organization_id, key, request_sha256, quote_id, revision_number
                        ) values (:org, 'illegal', :hash, :foreign, 1)
                    """), {"org": organization_a, "foreign": quote_b, "hash": "b" * 64})
                with pytest.raises(IntegrityError), connection.begin_nested():
                    connection.execute(text("""
                        insert into quote_revisions (
                            organization_id, quote_id, number, catalog_version_id,
                            snapshot, snapshot_sha256, is_complete, total_rub
                        ) values (:org, :quote, 2, :foreign, '{}'::jsonb, :hash, false, null)
                    """), {
                        "org": organization_a, "quote": quote_a,
                        "foreign": version_b, "hash": "b" * 64,
                    })

                for org, version, quote in (
                    (organization_a, version_a, quote_a),
                    (organization_b, version_b, quote_b),
                ):
                    connection.execute(text(
                        "select set_config('kpdesk.organization_id', :org, true)"
                    ), {"org": str(org)})
                    connection.execute(text("""
                        insert into idempotency_records (
                            organization_id, namespace, key, request_sha256, result_version_id
                        ) values (:org, 'import', 'same-key', :hash, :version)
                    """), {"org": org, "hash": "a" * 64, "version": version})
                    assert connection.scalar(text("""
                        select count(*) from idempotency_records
                        where namespace='import' and key='same-key'
                    """)) == 1
                    connection.execute(text("""
                        insert into catalog_active (organization_id, catalog_version_id)
                        values (:org, :version)
                    """), {"org": org, "version": version})
                    connection.execute(text("""
                        insert into quote_write_requests (
                            organization_id, key, request_sha256, quote_id, revision_number
                        ) values (:org, 'shared-key', :hash, :quote, 1)
                    """), {"org": org, "hash": "c" * 64, "quote": quote})
                    assert connection.scalar(text("select count(*) from catalog_active")) == 1
                    assert connection.scalar(text("""
                        select count(*) from quote_write_requests where key='shared-key'
                    """)) == 1
                connection.execute(text(
                    "select set_config('kpdesk.organization_id', '', true)"
                ))
                assert connection.scalar(text("select count(*) from catalog_versions")) == 0
                assert connection.scalar(text("select count(*) from idempotency_records")) == 0
                connection.execute(text("reset role"))
            finally:
                transaction.rollback()
        # Separate committed connection: test-only A3 changes MUST be absent.
        with engine.connect() as check:
            assert check.scalar(text("select version_num from alembic_version")) == (
                "0006_legacy_quarantine"
            )
            assert check.scalar(text("""
                select relrowsecurity from pg_class where oid='catalog_versions'::regclass
            """)) is False
            assert check.scalar(text("""
                select count(*) from catalog_versions where id=:version
            """), {"version": version_a}) == 0
    finally:
        engine.dispose()

def test_a3_refuses_production_before_any_ddl() -> None:
    """Read-only connection prevents accidental migration even if guard regresses."""
    settings = dotenv_values(Path(__file__).resolve().parents[2] / ".env")
    secret = settings.get("KP_DESK_DB_PASSWORD")
    assert isinstance(secret, str) and len(secret) >= 32
    engine = create_engine(URL.create(
        "postgresql+psycopg", username="kpdesk", password=secret,
        host="127.0.0.1", port=int(settings.get("KP_DESK_DB_PORT") or 55432),
        database="kpdesk",
    ), connect_args={"options": "-c default_transaction_read_only=on"})
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("select current_database()")) == "kpdesk"
            assert connection.scalar(text("select version_num from alembic_version")) == (
                "0031_catalog_taxonomy_v4"
            )
            migration = importlib.import_module(
                "migrations.versions.0007_tenant_contract_rls"
            )
            with (
                Operations.context(MigrationContext.configure(connection)),
                pytest.raises(RuntimeError, match="forbidden outside kpdesk_tenant_test"),
            ):
                migration.upgrade()
    finally:
        engine.dispose()
