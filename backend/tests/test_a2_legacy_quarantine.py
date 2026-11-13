"""A2 safety contract. Dedicated kpdesk_tenant_test only, never legacy pytest DB."""

from __future__ import annotations

import importlib
import uuid
from pathlib import Path
from types import ModuleType

import pytest
from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Connection, Engine
from sqlalchemy.exc import DatabaseError

TABLES = (
    "catalog_versions", "catalog_items", "catalog_active",
    "idempotency_records", "quote_documents", "quote_revisions", "quote_write_requests",
)


def _engine() -> Engine:
    conf = dotenv_values(Path(__file__).resolve().parents[2] / ".env")
    password = conf.get("KP_DESK_DB_PASSWORD")
    assert isinstance(password, str) and len(password) >= 32
    return create_engine(URL.create(
        "postgresql+psycopg", username="kpdesk", password=password,
        host="127.0.0.1", port=int(conf.get("KP_DESK_DB_PORT") or 55432),
        database="kpdesk_tenant_test",
    ))


def _migration() -> ModuleType:
    return importlib.import_module("migrations.versions.0006_legacy_quarantine")


def _assert_test_database(connection: Connection) -> None:
    assert connection.scalar(text("SELECT current_database()")) == "kpdesk_tenant_test"


def test_a2_all_legacy_domain_rows_are_quarantined_with_immutable_audit() -> None:
    engine = _engine()
    try:
        with engine.connect() as connection:
            _assert_test_database(connection)
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0006_legacy_quarantine"
            )
            audit = connection.execute(text("""
                SELECT organization_id, counts_before, counts_after,
                       rows_sha256_before, rows_sha256_after,
                       snapshots_sha256_before, snapshots_sha256_after
                FROM legacy_quarantine_audit
            """)).mappings().one()
            assert audit["counts_before"] == audit["counts_after"]
            assert set(audit["counts_before"]) == set(TABLES)
            assert audit["counts_before"]["catalog_versions"] >= 1
            assert audit["counts_before"]["quote_revisions"] >= 1
            assert audit["rows_sha256_before"] == audit["rows_sha256_after"]
            assert audit["snapshots_sha256_before"] == audit["snapshots_sha256_after"]
            assert len(audit["rows_sha256_before"]) == 64
            assert len(audit["snapshots_sha256_before"]) == 64
            assert connection.scalar(text(
                "SELECT status FROM organizations WHERE id=:org"
            ), {"org": audit["organization_id"]}) == "legacy_locked"
            assert connection.scalar(text(
                "SELECT count(*) FROM organization_memberships WHERE organization_id=:org"
            ), {"org": audit["organization_id"]}) == 0
            for table in TABLES:
                assert connection.scalar(text(
                    f"SELECT count(*) FROM {table} WHERE organization_id IS NULL"
                )) == 0, table
                assert connection.scalar(text(
                    f"SELECT count(*) FROM {table} WHERE organization_id != :org"
                ), {"org": audit["organization_id"]}) == 0, table
            assert connection.scalar(text(
                "SELECT tgenabled FROM pg_trigger WHERE tgname='quote_revision_no_update_delete'"
            )) == "O"
    finally:
        engine.dispose()


def test_a2_repeat_is_idempotent_without_audit_mutation() -> None:
    engine = _engine()
    try:
        with engine.connect() as connection:
            _assert_test_database(connection)
            migration = _migration()
            first = connection.execute(text(
                "SELECT counts_before, rows_sha256_before FROM legacy_quarantine_audit"
            )).one()
            audit_id = migration.quarantine_legacy_rows(connection)
            assert isinstance(audit_id, uuid.UUID)
            second = connection.execute(text(
                "SELECT counts_before, rows_sha256_before FROM legacy_quarantine_audit"
            )).one()
            assert first == second
            assert connection.scalar(text(
                "SELECT count(*) FROM legacy_quarantine_audit"
            )) == 1
    finally:
        engine.dispose()


def test_a2_detects_new_unscoped_legacy_rows_and_rolls_back() -> None:
    engine = _engine()
    try:
        with engine.connect() as connection:
            _assert_test_database(connection)
            tx = connection.get_transaction()
            assert tx is not None
            synthetic_id = uuid.uuid4()
            try:
                connection.execute(text("""
                    INSERT INTO catalog_versions (id, source_sha256, original_filename, rows_count)
                    VALUES (:id, :sha, 'A2 safety sentinel', 1)
                """), {"id": synthetic_id, "sha": "f" * 64})
                with pytest.raises(RuntimeError, match="unscoped"):
                    _migration().quarantine_legacy_rows(connection)
                assert connection.scalar(text(
                    "SELECT organization_id FROM catalog_versions WHERE id=:id"
                ), {"id": synthetic_id}) is None
            finally:
                tx.rollback()
            assert connection.scalar(text(
                "SELECT count(*) FROM catalog_versions WHERE id=:id"
            ), {"id": synthetic_id}) == 0
    finally:
        engine.dispose()


def test_a2_historical_revisions_and_audit_remain_immutable_after_migration() -> None:
    engine = _engine()
    try:
        with engine.connect() as connection:
            _assert_test_database(connection)
            quote = connection.execute(text(
                "SELECT quote_id, number FROM quote_revisions LIMIT 1"
            )).one()
            with pytest.raises(DatabaseError), connection.begin_nested():
                connection.execute(text("""
                    UPDATE quote_revisions SET organization_id=organization_id
                    WHERE quote_id=:id AND number=:number
                """), {"id": quote[0], "number": quote[1]})
            with pytest.raises(DatabaseError), connection.begin_nested():
                connection.execute(text(
                    "UPDATE legacy_quarantine_audit SET counts_after=counts_before"
                ))
    finally:
        engine.dispose()



def test_a2_audit_is_not_exposed_to_the_application_role() -> None:
    engine = _engine()
    try:
        with engine.connect() as connection:
            _assert_test_database(connection)
            assert connection.scalar(text(
                "SELECT has_table_privilege('kpdesk_app', 'legacy_quarantine_audit', 'SELECT')"
            )) is False
            assert connection.scalar(text(
                "SELECT has_table_privilege('kpdesk_app', 'legacy_quarantine_audit', 'UPDATE')"
            )) is False
    finally:
        engine.dispose()


def test_a2_detects_payload_tampering_and_rollback_restores_audit_integrity() -> None:
    engine = _engine()
    try:
        with engine.connect() as connection:
            _assert_test_database(connection)
            tx = connection.get_transaction()
            assert tx is not None
            version_id = connection.scalar(text(
                "SELECT id FROM catalog_versions ORDER BY id LIMIT 1"
            ))
            assert version_id is not None
            before = connection.scalar(text(
                "SELECT original_filename FROM catalog_versions WHERE id = :id"
            ), {"id": version_id})
            try:
                connection.execute(text(
                    "UPDATE catalog_versions SET original_filename='unauthorized mutation' "
                    "WHERE id = :id"
                ), {"id": version_id})
                with pytest.raises(RuntimeError, match="changed since the audit"):
                    _migration().quarantine_legacy_rows(connection)
            finally:
                tx.rollback()
            assert connection.scalar(text(
                "SELECT original_filename FROM catalog_versions WHERE id = :id"
            ), {"id": version_id}) == before
    finally:
        engine.dispose()
