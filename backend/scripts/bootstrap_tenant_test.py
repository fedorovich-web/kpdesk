"""Create/seed only the dedicated tenant-migration test database (never production).

Usage: python -m scripts.bootstrap_tenant_test create
       KP_DESK_DB_NAME=kpdesk_tenant_test alembic upgrade 0005_tenant_expand
       python -m scripts.bootstrap_tenant_test seed
       KP_DESK_DB_NAME=kpdesk_tenant_test alembic upgrade 0006_legacy_quarantine

No drop/truncate/reseed and no real supplier or personal data.
"""

from __future__ import annotations

import hashlib
import json
import sys
import uuid
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

TEST_DATABASE = "kpdesk_tenant_test"
_ROOT = Path(__file__).resolve().parents[2]


def _url(database: str) -> URL:
    conf = dotenv_values(_ROOT / ".env")
    password = conf.get("KP_DESK_DB_PASSWORD")
    if not isinstance(password, str) or len(password) < 32:
        raise RuntimeError("Local PostgreSQL administrator credentials unavailable.")
    return URL.create(
        "postgresql+psycopg",
        username="kpdesk", password=password,
        host="127.0.0.1", port=int(conf.get("KP_DESK_DB_PORT") or 55432),
        database=database,
    )


def create_database() -> None:
    engine = create_engine(_url("postgres"), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            exists = conn.scalar(text(
                "SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname = :name)"
            ), {"name": TEST_DATABASE})
            if exists:
                print(f"{TEST_DATABASE}: already exists; no changes")
                return
            conn.execute(text(f"CREATE DATABASE {TEST_DATABASE}"))
            print(f"{TEST_DATABASE}: created empty on local PostgreSQL")
    finally:
        engine.dispose()


def seed_legacy_fixtures() -> None:
    engine = create_engine(_url(TEST_DATABASE))
    try:
        with engine.begin() as conn:
            if conn.scalar(text("SELECT current_database()")) != TEST_DATABASE:
                raise RuntimeError("Refusing to seed another database.")
            revision = conn.scalar(text("SELECT version_num FROM alembic_version"))
            if revision != "0005_tenant_expand":
                raise RuntimeError("Seed requires exactly 0005_tenant_expand.")
            for table in (
                "catalog_versions", "catalog_items", "idempotency_records",
                "quote_documents", "quote_revisions", "quote_write_requests",
                "organizations", "users", "organization_memberships",
            ):
                if conn.scalar(text(f"SELECT count(*) FROM {table}")):
                    raise RuntimeError(f"Seed refused: {table} is not empty.")
            if conn.scalar(text(
                "SELECT count(*) FROM catalog_active "
                "WHERE id != 1 OR catalog_version_id IS NOT NULL"
            )):
                raise RuntimeError("Seed refused: non-empty active pointer.")

            cat_a = uuid.uuid5(uuid.NAMESPACE_URL, "kpdesk:test:catalog:a")
            cat_b = uuid.uuid5(uuid.NAMESPACE_URL, "kpdesk:test:catalog:b")
            quote = uuid.uuid5(uuid.NAMESPACE_URL, "kpdesk:test:quote:a")
            versions = (
                (cat_a, "1" * 64, "synthetic-office-v1.csv"),
                (cat_b, "2" * 64, "synthetic-office-v2.csv"),
            )
            for identifier, digest, name in versions:
                conn.execute(text("""
                    INSERT INTO catalog_versions
                    (id, source_sha256, original_filename, rows_count)
                    VALUES (:id, :sha, :name, 2)
                """), {"id": identifier, "sha": digest, "name": name})
            for version, sku, name, price in (
                (cat_a, "TEST-P-001", "Synthetic A4 80", "349.90"),
                (cat_a, "TEST-P-002", "Synthetic A3 100", "450.10"),
                (cat_b, "TEST-P-001", "Synthetic A4 80 v2", "359.90"),
                (cat_b, "TEST-P-003", "Synthetic A3 120 v2", "550.10"),
            ):
                conn.execute(text("""
                    INSERT INTO catalog_items (
                        catalog_version_id, sku, name, category, sale_unit,
                        pieces_per_sale_unit, price_rub, brand, paper_format,
                        paper_gsm, attributes
                    ) VALUES (
                        :version, :sku, :name, 'paper', 'пачка', 1, :price,
                        'Synthetic', 'A4', 80, '{}'::jsonb
                    )
                """), {"version": version, "sku": sku, "name": name, "price": price})

            if conn.scalar(text("SELECT count(*) FROM catalog_active")):
                conn.execute(text(
                    "UPDATE catalog_active SET catalog_version_id = :id WHERE id = 1"
                ), {"id": cat_a})
            else:
                conn.execute(text(
                    "INSERT INTO catalog_active (id, catalog_version_id) VALUES (1, :id)"
                ), {"id": cat_a})
            for idx, catalog_id in enumerate((cat_a, cat_b), start=1):
                conn.execute(text("""
                    INSERT INTO idempotency_records (
                        namespace, key, request_sha256, result_version_id
                    ) VALUES ('catalog', :key, :sha, :id)
                """), {"key": f"synthetic-test-{idx}", "sha": str(idx) * 64,
                       "id": catalog_id})
            conn.execute(text("""
                INSERT INTO quote_documents (id, current_revision) VALUES (:id, 1)
            """), {"id": quote})
            snapshot = {"test_fixture": "synthetic", "lines": [{"sku": "TEST-P-001"}]}
            body = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
            conn.execute(text("""
                INSERT INTO quote_revisions (
                    quote_id, number, catalog_version_id, snapshot,
                    snapshot_sha256, is_complete, total_rub
                ) VALUES (:id, 1, :catalog, CAST(:snapshot AS jsonb), :sha, TRUE, 349.90)
            """), {"id": quote, "catalog": cat_a, "snapshot": body,
                   "sha": hashlib.sha256(body.encode("utf-8")).hexdigest()})
            conn.execute(text("""
                INSERT INTO quote_write_requests (
                    key, request_sha256, quote_id, revision_number
                ) VALUES ('synthetic-quote-key', :sha, :id, 1)
            """), {"id": quote, "sha": "9" * 64})
            print(f"{TEST_DATABASE}: seeded synthetic two catalogs, four products, one quote")
    finally:
        engine.dispose()


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in ("create", "seed"):
        raise SystemExit("Usage: python -m scripts.bootstrap_tenant_test [create|seed]")
    if sys.argv[1] == "create":
        create_database()
    else:
        seed_legacy_fixtures()


if __name__ == "__main__":
    main()
