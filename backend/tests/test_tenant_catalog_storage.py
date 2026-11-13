"""A4 tenant catalog persistence, RLS + DB permissions, rolled back test-only.

Never migrate or mutate production kpdesk. The entire temporary A3 contract,
test organization/users/catalog writes and role GRANTs roll back atomically.
"""
from __future__ import annotations

import csv
import importlib
import io
import uuid
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError

from kpdesk.catalog.parse import parse_price_list
from kpdesk.catalog.schema import COLUMNS_V2, CatalogImportResult, ImportLimits
from kpdesk.catalog.storage import CatalogImportConflict, InvalidCatalog
from kpdesk.catalog.tenant_storage import TenantCatalogRepository
from kpdesk.db.tenant_access import AuthenticatedUser, TenantAccessDenied

ROOT = Path(__file__).resolve().parents[2]


def _source(sku: str, amount: str) -> CatalogImportResult:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=";", lineterminator="\n")
    writer.writerow(COLUMNS_V2)
    row: dict[str, str | int] = {
        "sku": sku, "name": "Бумага А4 80 г/м2", "category": "paper",
        "sale_unit": "пачка", "pieces_per_sale_unit": 1,
        "price_rub": amount, "brand": "Paper",
        "paper_format": "A4", "paper_gsm": 80, "attributes_json": "{}",
    }
    writer.writerow([row.get(key, "") for key in COLUMNS_V2])
    result = parse_price_list("tenant-test.csv", stream.getvalue().encode("utf-8"), ImportLimits())
    assert result.publishable, result.errors
    return result


def test_a4_postgres_catalog_two_tenants_and_role_guards_rollback() -> None:
    credentials = dotenv_values(ROOT / ".env")
    secret = credentials.get("KP_DESK_DB_PASSWORD")
    assert isinstance(secret, str) and len(secret) >= 32
    engine = create_engine(URL.create(
        "postgresql+psycopg", username="kpdesk", password=secret,
        host="127.0.0.1", port=int(credentials.get("KP_DESK_DB_PORT") or 55432),
        database="kpdesk_tenant_test",
    ))
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    alice, bob, viewer = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    version_a: uuid.UUID | None = None
    try:
        with engine.connect() as connection:
            tx = connection.begin()
            try:
                assert connection.scalar(text("SELECT current_database()")) == (
                    "kpdesk_tenant_test"
                )
                assert connection.scalar(text(
                    "SELECT version_num FROM alembic_version"
                )) == "0006_legacy_quarantine"
                a3 = importlib.import_module("migrations.versions.0007_tenant_contract_rls")
                with Operations.context(MigrationContext.configure(connection)):
                    a3.upgrade()
                for org, label in ((org_a, "A"), (org_b, "B")):
                    connection.execute(text("""
                        INSERT INTO organizations (id, name, status)
                        VALUES (:id, :name, 'active')
                    """), {"id": org, "name": "A4 test org " + label})
                for person, label in ((alice, "alice"), (bob, "bob"), (viewer, "viewer")):
                    connection.execute(text("""
                        INSERT INTO users(id, email, status, email_verified_at)
                        VALUES (:id, :email, 'active', now())
                    """), {"id": person, "email": label + "@a4-test.invalid"})
                for org, person, role in (
                    (org_a, alice, "owner"), (org_b, bob, "owner"),
                    (org_a, viewer, "viewer"),
                ):
                    connection.execute(text("""
                        INSERT INTO organization_memberships
                            (organization_id, user_id, role, status)
                        VALUES (:org, :person, :role, 'active')
                    """), {"org": org, "person": person, "role": role})

                # Test-only grants. Prod must use a reviewed least-privilege
                # membership authorization boundary, NOT grant free user access.
                connection.execute(text("GRANT USAGE ON SCHEMA public TO kpdesk_app"))
                for table in ("organizations", "users", "organization_memberships"):
                    connection.exec_driver_sql(
                        f"GRANT SELECT ON TABLE public.{table} TO kpdesk_app"
                    )
                for table in (
                    "catalog_versions", "catalog_items", "idempotency_records",
                ):
                    connection.exec_driver_sql(
                        f"GRANT SELECT, INSERT ON TABLE public.{table} TO kpdesk_app"
                    )
                # Only the active pointer may be updated. Catalog versions,
                # items and idempotency history remain insert-only for app role.
                connection.execute(text(
                    "GRANT SELECT, INSERT, UPDATE ON TABLE "
                    "public.catalog_active TO kpdesk_app"
                ))
                connection.execute(text("SET ROLE kpdesk_app"))
                assert connection.scalar(text("SELECT current_user")) == "kpdesk_app"

                alice_a = TenantCatalogRepository(
                    connection, AuthenticatedUser(alice), org_a,
                )
                bob_b = TenantCatalogRepository(
                    connection, AuthenticatedUser(bob), org_b,
                )
                viewer_a = TenantCatalogRepository(
                    connection, AuthenticatedUser(viewer), org_a,
                )
                first = _source("SKU-A", "399.90")
                second = _source("SKU-B", "499.90")
                assert alice_a.current() is None
                assert bob_b.current() is None

                published_a = alice_a.publish(
                    first, "same-key", first.source_sha256,
                )
                version_a = published_a.id
                assert alice_a.current() == published_a
                assert [p.sku for p in alice_a.items(published_a.id)] == ["SKU-A"]
                with pytest.raises(DBAPIError), connection.begin_nested():
                    connection.execute(text("""
                        UPDATE catalog_versions
                        SET source_sha256 = :tampered WHERE id = :id
                    """), {"tampered": "0" * 64, "id": published_a.id})
                with pytest.raises(DBAPIError), connection.begin_nested():
                    connection.execute(text("""
                        DELETE FROM catalog_items WHERE catalog_version_id = :id
                    """), {"id": published_a.id})
                assert alice_a.publish(
                    first, "same-key", first.source_sha256,
                ).id == published_a.id
                with pytest.raises(CatalogImportConflict):
                    alice_a.publish(second, "same-key", second.source_sha256)
                assert alice_a.current() == published_a

                published_b = bob_b.publish(
                    second, "same-key", second.source_sha256,
                )
                assert published_a.id != published_b.id
                assert bob_b.current() == published_b
                assert [p.sku for p in bob_b.items(published_b.id)] == ["SKU-B"]
                assert bob_b.items(published_a.id) == []
                assert alice_a.items(published_b.id) == []
                assert alice_a.current() == published_a

                assert [p.sku for p in viewer_a.items(published_a.id)] == ["SKU-A"]
                with pytest.raises(TenantAccessDenied):
                    viewer_a.publish(second, "viewer-key", second.source_sha256)
                assert connection.scalar(text(
                    "SELECT current_setting('kpdesk.organization_id', true)"
                )) in (None, "")

                denied_cross = TenantCatalogRepository(
                    connection, AuthenticatedUser(alice), org_b,
                )
                with pytest.raises(TenantAccessDenied):
                    denied_cross.current()
                assert connection.scalar(text(
                    "SELECT current_setting('kpdesk.organization_id', true)"
                )) in (None, "")

                alice_a_after = alice_a.publish(
                    second, "new-key", second.source_sha256,
                )
                assert alice_a_after.id != published_a.id
                assert alice_a.current() == alice_a_after
                assert [p.sku for p in alice_a.items(published_a.id)] == ["SKU-A"]
                assert [p.sku for p in alice_a.items(alice_a_after.id)] == ["SKU-B"]
                assert bob_b.current() == published_b

                invalid = parse_price_list("bad.csv", b"not;canonical\none;two\n", ImportLimits())
                assert not invalid.publishable
                with pytest.raises(InvalidCatalog):
                    alice_a.publish(invalid, "invalid", invalid.source_sha256)
                assert alice_a.current() == alice_a_after

                connection.execute(text("RESET ROLE"))
            finally:
                tx.rollback()
        with engine.connect() as verify:
            assert verify.scalar(text(
                "SELECT version_num FROM alembic_version"
            )) == "0006_legacy_quarantine"
            assert verify.scalar(text(
                "SELECT relrowsecurity FROM pg_class "
                "WHERE oid='catalog_versions'::regclass"
            )) is False
            assert version_a is not None
            assert verify.scalar(text(
                "SELECT count(*) FROM catalog_versions WHERE id = :id"
            ), {"id": version_a}) == 0
            assert verify.scalar(text(
                "SELECT count(*) FROM organizations WHERE id = :org"
            ), {"org": org_a}) == 0
    finally:
        engine.dispose()


def test_a4_repository_refuses_ddl_free_owner_connection() -> None:
    credentials = dotenv_values(ROOT / ".env")
    secret = credentials.get("KP_DESK_DB_PASSWORD")
    assert isinstance(secret, str) and len(secret) >= 32
    engine = create_engine(URL.create(
        "postgresql+psycopg", username="kpdesk", password=secret,
        host="127.0.0.1", port=int(credentials.get("KP_DESK_DB_PORT") or 55432),
        database="kpdesk",
    ), connect_args={"options": "-c default_transaction_read_only=on"})
    try:
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT current_database()")) == "kpdesk"
            repo = TenantCatalogRepository(
                conn, AuthenticatedUser(uuid.uuid4()), uuid.uuid4(),
            )
            with pytest.raises(RuntimeError, match="restricted tenant role"):
                repo.current()
    finally:
        engine.dispose()
