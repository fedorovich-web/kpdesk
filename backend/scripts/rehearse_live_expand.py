"""Apply real additive release migration ONLY to a tagged restored DB, then clean it."""
from __future__ import annotations

import hashlib
import importlib
import json
import secrets
import uuid
from pathlib import Path

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from dotenv import dotenv_values
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool

from scripts.rehearse_release_snapshot import (
    ROOT,
    _docker_input,
    _engine,
    _read_snapshot,
)

_MANIFEST = (
    ROOT / "backups" / "2026-10-10-a12-preflight"
    / "kpdesk-0005-2c31e7b4ef14.manifest.json"
)
_MIGRATION = importlib.import_module("migrations.versions.0013_live_expand")
_TABLES = (
    "tenant_catalog_active", "tenant_catalog_idempotency",
    "tenant_quote_write_requests",
)
_REQUIRED = (
    "fk_live_item_scoped_catalog", "fk_live_import_scoped_catalog",
    "fk_live_revision_scoped_document", "fk_live_revision_scoped_catalog",
    "fk_live_quote_write_scoped_document", "uq_live_catalog_org_id",
    "uq_live_document_org_id", "uq_live_revision_org_key",
)


def verify() -> None:
    document = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    if document.get("status") != "clone_rehearsal_passed_not_production_release":
        raise RuntimeError("Only a proven verified A12 backup is allowed.")
    dump = Path(document["backup_path"])
    with dump.open("rb") as handle:
        actual_hash = hashlib.file_digest(handle, "sha256").hexdigest()
    if actual_hash != document["backup_sha256"]:
        raise RuntimeError("Verified backup SHA256 mismatch.")
    env = dotenv_values(ROOT / ".env")
    password = env.get("KP_DESK_DB_PASSWORD")
    if not isinstance(password, str) or len(password) < 32:
        raise RuntimeError("Local owner credentials unavailable.")
    port = int(env.get("KP_DESK_DB_PORT") or 55432)
    live = _engine("kpdesk", password, port)
    admin = _engine("postgres", password, port, admin=True)
    nonce = secrets.token_hex(6)
    name = "kpdesk_live_expand_" + nonce
    marker = "kpdesk:live-expand-rehearsal:" + nonce
    clone = None
    created = False
    try:
        before = _read_snapshot(live, "kpdesk")
        if before.schema_sha256 != document["schema_sha256"]:
            raise RuntimeError("Production schema differs from restored backup.")
        if before.counts != document["source_counts"]:
            raise RuntimeError("Production counts differ from restored backup.")
        with admin.connect() as conn:
            if conn.scalar(text("SELECT current_database()")) != "postgres":
                raise RuntimeError("Wrong administrative database.")
            if conn.scalar(text("""
                SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=:db)
            """), {"db": name}):
                raise RuntimeError("Generated rehearsal DB name occupied.")
            conn.exec_driver_sql(f"CREATE DATABASE {name} TEMPLATE template0")
            created = True
            conn.exec_driver_sql(f"COMMENT ON DATABASE {name} IS '{marker}'")
        _docker_input(
            "pg_restore", "-U", "kpdesk", "--dbname=" + name,
            "--single-transaction", "--exit-on-error", "--no-owner", "--no-acl",
            input_file=dump,
        )
        clone = create_engine(
            URL.create(
                "postgresql+psycopg", username="kpdesk", password=password,
                host="127.0.0.1", port=port, database=name,
            ),
            poolclass=NullPool, pool_pre_ping=True,
            connect_args={"connect_timeout": 4},
        )
        restored = _read_snapshot(clone, name)
        if restored != before:
            raise RuntimeError("Rehearsal copy differs from live source baseline.")
        with clone.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                _MIGRATION.upgrade()
            conn.execute(text("""
                UPDATE alembic_version SET version_num='0013_live_expand'
                WHERE version_num='0005_tenant_expand'
            """))
        with clone.connect() as conn:
            if conn.scalar(text("SELECT version_num FROM alembic_version")) != (
                "0013_live_expand"
            ):
                raise RuntimeError("Migration revision not committed on clone.")
            inspector = inspect(conn)
            if not set(_TABLES).issubset(set(inspector.get_table_names())):
                raise RuntimeError("Release tables missing after migration.")
            all_constraints: set[str] = set()
            for table in (
                "catalog_versions", "catalog_items", "idempotency_records",
                "quote_documents", "quote_revisions", "quote_write_requests",
            ):
                for kind in (
                    inspector.get_foreign_keys(table),
                    inspector.get_unique_constraints(table),
                ):
                    all_constraints.update(row["name"] for row in kind if row["name"] is not None)
            if not set(_REQUIRED).issubset(all_constraints):
                raise RuntimeError("Scoped constraints missing.")
            if conn.scalar(text("SELECT catalog_version_id FROM catalog_active WHERE id=1")):
                raise RuntimeError("Legacy active pointer was changed.")
            for table in _TABLES:
                if conn.scalar(text(f"SELECT count(*) FROM {table}")):
                    raise RuntimeError("New tenant tables unexpectedly nonempty.")
                if conn.scalar(text("""
                    SELECT has_table_privilege('kpdesk_app', :table, 'SELECT')
                """), {"table": "public." + table}):
                    raise RuntimeError("Unreleased tenant table privileges exposed.")

        # Real enforced composite FK, tested in a rolled-back transaction only.
        with clone.connect() as conn:
            tx = conn.begin()
            try:
                org_a, org_b, version = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
                for org, label in ((org_a, "test-a"), (org_b, "test-b")):
                    conn.execute(text("""
                        INSERT INTO organizations (id,name,status)
                        VALUES (:id,:name,'active')
                    """), {"id": org, "name": label})
                conn.execute(text("""
                    INSERT INTO catalog_versions
                        (id, organization_id, source_sha256, original_filename, rows_count)
                    VALUES (:id,:org,:hash,'fixture.csv',1)
                """), {"id": version, "org": org_a, "hash": "a"*64})
                try:
                    with conn.begin_nested():
                        conn.execute(text("""
                            INSERT INTO tenant_catalog_active
                                (organization_id,catalog_version_id)
                            VALUES (:org,:version)
                        """), {"org": org_b, "version": version})
                except IntegrityError:
                    pass
                else:
                    raise RuntimeError("Cross-tenant catalog pointer was accepted.")
                conn.execute(text("""
                    INSERT INTO tenant_catalog_active
                        (organization_id,catalog_version_id)
                    VALUES (:org,:version)
                """), {"org": org_a, "version": version})
            finally:
                tx.rollback()

        if _read_snapshot(live, "kpdesk") != before:
            raise RuntimeError("Production source changed during rehearsal.")
        print("LIVE_EXPAND_REHEARSAL_PASSED original_unchanged=true "
              "legacy_singleton_preserved=true tenant_tables=3 "
              "scoped_fks_enforced=true unprivileged_runtime_denied=true")
    finally:
        if clone is not None:
            clone.dispose()
        try:
            if created:
                with admin.connect() as conn:
                    verified = conn.scalar(text("""
                        SELECT shobj_description(oid,'pg_database')
                        FROM pg_database WHERE datname=:db
                    """), {"db": name})
                    if verified != marker:
                        raise RuntimeError("Cannot safely delete unknown rehearsal DB.")
                    conn.exec_driver_sql(f"DROP DATABASE {name} WITH (FORCE)")
        finally:
            live.dispose()
            admin.dispose()


if __name__ == "__main__":
    verify()
