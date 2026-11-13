"""A12: guarded live-snapshot backup/restore rehearsal, never a production migration.

Read-only pg_dump of the existing local kpdesk and read-only source fingerprints.
The only DDL runs on a uniquely named, owner-marked disposable restored database.
No Alembic upgrade, production writes, fake tenant ownership or Git operations.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.pool import NullPool

from kpdesk.matching.profiles import CATEGORY_ATTRIBUTES

ROOT = Path(__file__).resolve().parents[2]
CONTAINER = "kpdesk-db-1"
SOURCE_DATABASE = "kpdesk"
REVISION = "0005_tenant_expand"
TABLES = (
    "alembic_version", "catalog_versions", "catalog_items", "catalog_active",
    "idempotency_records", "quote_documents", "quote_revisions",
    "quote_write_requests", "organizations", "users", "organization_memberships",
)
GENERATED = re.compile(r"kpdesk_release_e2e_[0-9a-f]{12}\Z")
PRIVATE_TABLES = (
    "catalog_versions", "catalog_items", "idempotency_records",
    "quote_documents", "quote_revisions", "quote_write_requests",
    "organizations", "users", "organization_memberships",
)


def _require_empty_sensitive_sources(counts: dict[str, int]) -> None:
    """Never persist an unencrypted rehearsal dump containing actual customers."""
    if any(counts.get(table, 0) != 0 for table in PRIVATE_TABLES):
        raise RuntimeError(
            "A12 unencrypted backup is forbidden with customer/commercial rows; "
            "implement independently restorable encrypted backup first."
        )
ENUM_CHECKS: dict[tuple[str, str], tuple[str, tuple[str, ...]]] = {
    ("catalog_items", "valid_catalog_category"): (
        "category", tuple(CATEGORY_ATTRIBUTES),
    ),
    ("organization_memberships", "ck_membership_role"): (
        "role", ("owner", "editor", "viewer"),
    ),
    ("organization_memberships", "ck_membership_status"): (
        "status", ("active", "revoked"),
    ),
    ("organizations", "ck_organization_status"): (
        "status", ("active", "legacy_locked", "suspended"),
    ),
    ("users", "ck_user_status"): (
        "status", ("pending", "active", "disabled"),
    ),
}
_ENUM_ORIGINAL = re.compile(
    r"CHECK \(\(\((\w+)\)::text = ANY \(\(ARRAY\[(.+)\]\)::text\[\]\)\)\)"
)
_ENUM_RESTORED = re.compile(
    r"CHECK \(\(\((\w+)\)::text = ANY \(ARRAY\[(.+)\]\)\)\)"
)


def _canonical_constraint(
    table: str, name: str, kind: str, definition: str,
) -> tuple[str, str, str, str]:
    """Normalize only the verified PostgreSQL 17 enum-array rewrite.

    Every allowed check must reference the expected column and an exact,
    ordered list of allowed values. All other constraint SQL is unchanged.
    """
    expected = ENUM_CHECKS.get((table, name))
    if expected is None:
        return table, name, kind, definition
    if kind != "c":
        raise RuntimeError(f"Known CHECK constraint changed type: {table}.{name}")
    found = _ENUM_ORIGINAL.fullmatch(definition)
    restored = False
    if found is None:
        found = _ENUM_RESTORED.fullmatch(definition)
        restored = True
    if found is None or found.group(1) != expected[0]:
        raise RuntimeError(f"CHECK expression unexpectedly changed: {table}.{name}")
    source_token = re.compile(r"'([a-z_]+)'::character varying")
    restored_token = re.compile(r"\('([a-z_]+)'::character varying\)::text")
    token = restored_token if restored else source_token
    items = found.group(2).split(", ")
    values: list[str] = []
    for item in items:
        match = token.fullmatch(item)
        if match is None:
            raise RuntimeError(f"Invalid CHECK list expression: {table}.{name}")
        values.append(match.group(1))
    if tuple(values) != expected[1]:
        raise RuntimeError(f"CHECK accepted values changed: {table}.{name}")
    return table, name, kind, f"CHECK_ENUM({expected[0]},{','.join(values)})"


@dataclass(frozen=True)
class Snapshot:
    revision: str
    counts: dict[str, int]
    row_sha256: dict[str, str]
    schema_sha256: str
    schema_parts_sha256: dict[str, str]
    constraint_details: tuple[tuple[str, str, str, str], ...]
    immutable_quote_trigger: str


def _require_generated(database: str) -> None:
    if not GENERATED.fullmatch(database):
        raise RuntimeError("Refusing unrecognized release rehearsal database name.")


def _docker_input(*args: str, input_file: Path) -> str:
    """Feed binary custom dumps to Docker without PowerShell text conversion."""
    with input_file.open("rb") as stream:
        result = subprocess.run(
            ["docker", "exec", "-i", CONTAINER, *args],
            stdin=stream, capture_output=True, check=True, timeout=120,
        )
    return result.stdout.decode("utf-8", errors="replace")


def _engine(database: str, password: str, port: int, *, admin: bool = False) -> Engine:
    if database not in ("kpdesk", "postgres"):
        _require_generated(database)
    return create_engine(
        URL.create(
            "postgresql+psycopg", username="kpdesk", password=password,
            host="127.0.0.1", port=port, database=database,
        ),
        isolation_level="AUTOCOMMIT" if admin else None,
        poolclass=NullPool, pool_pre_ping=True,
        connect_args={"connect_timeout": 4},
    )


def _stable_digest(data: object) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, ensure_ascii=False,
                   separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _snapshot(connection: Connection, *, database: str) -> Snapshot:
    if connection.scalar(text("SELECT current_database()")) != database:
        raise RuntimeError("Database identity mismatch during A12 audit.")
    revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
    if revision != REVISION:
        raise RuntimeError("Release clone/source must be exactly revision 0005.")
    metadata = connection.execute(text("""
        SELECT table_name, column_name, udt_name, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema='public'
        ORDER BY table_name, ordinal_position
    """)).all()
    constraints = connection.execute(text("""
        SELECT conrelid::regclass::text, conname, contype,
               pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE connamespace='public'::regnamespace
        ORDER BY 1, 2
    """)).all()
    canonical_constraints = [
        _canonical_constraint(str(row[0]), str(row[1]),
                              str(row[2]), str(row[3]))
        for row in constraints
    ]
    indexes = connection.execute(text("""
        SELECT tablename, indexname, indexdef
        FROM pg_indexes WHERE schemaname='public'
        ORDER BY tablename, indexname
    """)).all()
    triggers = connection.execute(text("""
        SELECT tgrelid::regclass::text, tgname, tgenabled,
               pg_get_triggerdef(oid)
        FROM pg_trigger
        WHERE NOT tgisinternal AND tgrelid IN (
            SELECT oid FROM pg_class WHERE relnamespace='public'::regnamespace
        )
        ORDER BY 1, 2
    """)).all()
    immutable_trigger = connection.scalar(text("""
        SELECT tgenabled FROM pg_trigger
        WHERE tgrelid='public.quote_revisions'::regclass
          AND tgname='quote_revision_no_update_delete' AND NOT tgisinternal
    """))
    if immutable_trigger != "O":
        raise RuntimeError("Historical quote immutability trigger is not active.")
    if connection.scalar(text("""
        SELECT count(*) FROM catalog_active WHERE id <> 1
    """)):
        raise RuntimeError("Unexpected legacy singleton pointer.")
    counts: dict[str, int] = {}
    row_sha256: dict[str, str] = {}
    for table in TABLES:
        # Only fixed, compiled-in table names are interpolated into SQL.
        hasher = hashlib.sha256()
        count = 0
        statement = text(
            f"SELECT to_jsonb(t)::text FROM public.{table} AS t "
            f"ORDER BY to_jsonb(t)::text"
        )
        for (row,) in connection.execution_options(
            stream_results=True,
        ).execute(statement):
            if not isinstance(row, str):
                raise TypeError("Unexpected source row serialization.")
            payload = row.encode("utf-8")
            hasher.update(len(payload).to_bytes(8, "big"))
            hasher.update(payload)
            count += 1
        counts[table] = count
        row_sha256[table] = hasher.hexdigest()
    if counts["catalog_active"] != 1:
        raise RuntimeError("Legacy active pointer is missing or duplicated.")
    return Snapshot(
        revision=revision, counts=counts, row_sha256=row_sha256,
        schema_sha256=_stable_digest({
            "columns": metadata, "constraints": canonical_constraints,
            "indexes": indexes, "triggers": triggers,
        }),
        schema_parts_sha256={
            "columns": _stable_digest(metadata),
            "constraints": _stable_digest(canonical_constraints),
            "indexes": _stable_digest(indexes),
            "triggers": _stable_digest(triggers),
        },
        constraint_details=tuple(canonical_constraints),
        immutable_quote_trigger=str(immutable_trigger),
    )


def _read_snapshot(engine: Engine, database: str) -> Snapshot:
    with engine.connect() as connection:
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        return _snapshot(connection, database=database)


def _probe_additive_indexes(clone: Engine) -> None:
    """Only transactional DDL, rolled back: no speculative release migration."""
    names = (
        ("a12_probe_catalog_version_org_id", "catalog_versions", "organization_id, id"),
        ("a12_probe_quote_document_org_id", "quote_documents", "organization_id, id"),
        ("a12_probe_quote_revision_org_key", "quote_revisions",
         "organization_id, quote_id, number"),
    )
    with clone.connect() as connection:
        transaction = connection.begin()
        try:
            for name, table, columns in names:
                connection.exec_driver_sql(
                    f"CREATE UNIQUE INDEX {name} ON public.{table} ({columns})"
                )
                if connection.scalar(text("""
                    SELECT to_regclass(:relation) IS NOT NULL
                """), {"relation": "public." + name}) is not True:
                    raise RuntimeError("Clone-only additive index probe failed.")
        finally:
            transaction.rollback()
    with clone.connect() as connection:
        for name, _, _ in names:
            if connection.scalar(text("""
                SELECT to_regclass(:relation)
            """), {"relation": "public." + name}) is not None:
                raise RuntimeError("Rolled-back probe index persisted.")


def _create_clone(admin: Engine, database: str, marker: str) -> None:
    _require_generated(database)
    with admin.connect() as conn:
        if conn.scalar(text("""
            SELECT rolsuper FROM pg_roles WHERE rolname=current_user
        """)) is not True:
            raise RuntimeError("Disposable DB creation requires the existing owner.")
        if conn.scalar(text("""
            SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=:name)
        """), {"name": database}):
            raise RuntimeError("Generated database already exists; refusing reuse.")
        conn.exec_driver_sql(f"CREATE DATABASE {database} TEMPLATE template0")
        conn.exec_driver_sql(f"COMMENT ON DATABASE {database} IS '{marker}'")


def _drop_marked_clone(admin: Engine, database: str, marker: str) -> None:
    _require_generated(database)
    with admin.connect() as conn:
        actual = conn.scalar(text("""
            SELECT shobj_description(oid, 'pg_database')
            FROM pg_database WHERE datname=:name
        """), {"name": database})
        if actual != marker:
            raise RuntimeError("Refusing cleanup: clone marker missing/mismatched.")
        conn.exec_driver_sql(f"DROP DATABASE {database} WITH (FORCE)")


def rehearse() -> Path:
    env = dotenv_values(ROOT / ".env")
    password = env.get("KP_DESK_DB_PASSWORD")
    if not isinstance(password, str) or len(password) < 32:
        raise RuntimeError("Existing local owner credentials unavailable.")
    port = int(env.get("KP_DESK_DB_PORT") or 55432)
    nonce = secrets.token_hex(6)
    clone_name = "kpdesk_release_e2e_" + nonce
    backup_dir = ROOT / "backups" / "2026-10-10-a12-preflight"
    backup_dir.mkdir(parents=True, exist_ok=True)
    dump = backup_dir / f"kpdesk-0005-{nonce}.dump"
    report_path = backup_dir / f"kpdesk-0005-{nonce}.manifest.json"
    source = _engine(SOURCE_DATABASE, password, port)
    admin = _engine("postgres", password, port, admin=True)
    clone: Engine | None = None
    created = False
    marker = "kpdesk:a12-release-clone:" + nonce
    report: dict[str, object] | None = None
    try:
        with admin.connect() as conn:
            if conn.scalar(text("SELECT current_database()")) != "postgres":
                raise RuntimeError("Administrative connection is not postgres.")
            if conn.scalar(text("""
                SELECT rolsuper FROM pg_roles WHERE rolname=current_user
            """)) is not True:
                raise RuntimeError("A12 requires the existing local owner role.")
        first = _read_snapshot(source, SOURCE_DATABASE)
        _require_empty_sensitive_sources(first.counts)
        # pg_dump reads the production DB but never executes DDL/DML there.
        with dump.open("xb") as stream:
            command = [
                "docker", "exec", CONTAINER, "pg_dump", "-U", "kpdesk",
                "--dbname=kpdesk", "--format=custom", "--no-owner", "--no-acl",
                "--serializable-deferrable",
            ]
            subprocess.run(
                command, stdout=stream, stderr=subprocess.PIPE,
                check=True, timeout=180,
            )
        if dump.stat().st_size < 100:
            raise RuntimeError("Unexpectedly small backup; refusing restore.")
        with dump.open("rb") as backup_stream:
            content_sha = hashlib.file_digest(backup_stream, "sha256").hexdigest()
        listing = _docker_input("pg_restore", "--list", input_file=dump)
        if "TABLE DATA" not in listing or "alembic_version" not in listing:
            raise RuntimeError("Custom dump TOC lacks required table data.")
        after_dump = _read_snapshot(source, SOURCE_DATABASE)
        if first != after_dump:
            raise RuntimeError("Source changed across dump; refuse clone rehearsal.")
        _create_clone(admin, clone_name, marker)
        created = True
        _docker_input(
            "pg_restore", "--dbname=" + clone_name, "-U", "kpdesk",
            "--exit-on-error", "--single-transaction", "--no-owner", "--no-acl",
            input_file=dump,
        )
        clone = _engine(clone_name, password, port)
        restored = _read_snapshot(clone, clone_name)
        if restored != first:
            mismatched_tables = [
                table for table in TABLES
                if restored.row_sha256[table] != first.row_sha256[table]
            ]
            differing_schema_parts = [
                key for key in first.schema_parts_sha256
                if first.schema_parts_sha256[key] != restored.schema_parts_sha256[key]
            ]
            missing_constraint_definitions = sorted(
                set(first.constraint_details) - set(restored.constraint_details)
            )
            extra_constraint_definitions = sorted(
                set(restored.constraint_details) - set(first.constraint_details)
            )
            raise RuntimeError(
                "Restored clone differs: "
                f"missing_constraints={missing_constraint_definitions} "
                f"added_constraints={extra_constraint_definitions} "
                f"schema_parts={differing_schema_parts} "
                f"counts_equal={restored.counts == first.counts} "
                f"row_mismatches={mismatched_tables} "
                f"trigger_equal={restored.immutable_quote_trigger == first.immutable_quote_trigger}"
            )
        _probe_additive_indexes(clone)
        post_probe = _read_snapshot(clone, clone_name)
        if post_probe != restored:
            raise RuntimeError("Transactional index rehearsal altered restored data/schema.")
        final_source = _read_snapshot(source, SOURCE_DATABASE)
        if final_source != first:
            raise RuntimeError("Production source changed during A12; mark report invalid.")
        report = {
            "created_at_utc": datetime.now(UTC).isoformat(),
            "status": "clone_rehearsal_passed_not_production_release",
            "source_database": SOURCE_DATABASE,
            "source_revision": REVISION,
            "backup_path": str(dump),
            "backup_size_bytes": dump.stat().st_size,
            "backup_sha256": content_sha,
            "source_counts": first.counts,
            "source_row_sha256": first.row_sha256,
            "schema_sha256": first.schema_sha256,
            "restored_clone": clone_name,
            "restored_revision": restored.revision,
            "clone_exact_match": True,
            "transactional_additive_index_probe_rolled_back": True,
            "immutability_trigger_enabled": True,
            "production_ddl_performed": False,
            "test_only_migrations_applied": False,
            "snapshot_restoration_verified": True,
            "note": "A12 backup/restore preflight only; no tenant cutover or public auth.",
        }
    finally:
        if clone is not None:
            clone.dispose()
        try:
            if created:
                _drop_marked_clone(admin, clone_name, marker)
        finally:
            admin.dispose()
            source.dispose()
            if report is None and dump.exists():
                # Failed or unverified one-off dumps must not accumulate.
                dump.unlink()
    if report is None:
        raise RuntimeError("No verified backup manifest is available.")
    # Only claim success AFTER the temporary database has been dropped.
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    print(
        "A12_FRESH_PRODUCTION_SNAPSHOT_VERIFIED "
        f"source_revision={REVISION} archive_bytes={dump.stat().st_size} "
        f"dump_sha256={report['backup_sha256']} schema_match=true rows_match=true "
        "probe_indexes_rolled_back=true production_unchanged=true "
        f"manifest={report_path}"
    )
    return report_path


if __name__ == "__main__":
    rehearse()
