"""Safely upgrade EMPTY local KPDesk database 0030 -> reviewed 0031.

Never modifies a remote/production server. Requires an independently
verified local pg_dump backup and an explicit --apply flag.
No Git, application deployment or automatic migration to future heads.
"""
from __future__ import annotations

import argparse
import hashlib
import re
from collections.abc import Sequence
from pathlib import Path

from alembic import command
from alembic.config import Config
from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from scripts.audit_migration_lineage import audit_migration_lineage

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _ROOT / "backend"
_OLD = "0030_tenant_llm_cache"
_NEW = "0031_catalog_taxonomy_v4"
_BASELINE_COUNTS = (0, 0, 0, 0, 0)


def _read_only_preflight() -> None:
    lineage = audit_migration_lineage()
    if lineage.release_head != _NEW:
        raise RuntimeError("Release branch does not have the approved 0031 head")
    settings = dotenv_values(_ROOT / ".env")
    secret = settings.get("KP_DESK_DB_PASSWORD")
    if not isinstance(secret, str) or len(secret) < 32:
        raise RuntimeError("Local PostgreSQL owner credentials missing")
    engine = create_engine(
        URL.create(
            "postgresql+psycopg",
            username="kpdesk", password=secret, host="127.0.0.1",
            port=int(settings.get("KP_DESK_DB_PORT") or 55432),
            database="kpdesk",
        ), connect_args={"options": "-c default_transaction_read_only=on"},
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            version, database, owner = connection.execute(text("""
                SELECT (SELECT version_num FROM public.alembic_version),
                       current_database(),current_user
            """)).one()
            if database != "kpdesk" or owner != "kpdesk" or version != _OLD:
                raise RuntimeError("Refusing non-local, non-owner, or non-0030 database")
            counts = tuple(connection.execute(text("""
                SELECT (SELECT count(*) FROM public.organizations),
                       (SELECT count(*) FROM public.users),
                       (SELECT count(*) FROM public.catalog_items),
                       (SELECT count(*) FROM public.quote_documents),
                       (SELECT count(*) FROM public.tenant_llm_attempts)
            """)).one())
            if counts != _BASELINE_COUNTS:
                raise RuntimeError("Refusing migration of non-empty application tables")
            definition = connection.scalar(text("""
                SELECT pg_get_constraintdef(oid) FROM pg_constraint
                WHERE conrelid='public.catalog_items'::regclass
                  AND conname='valid_catalog_category' AND contype='c'
            """))
            from importlib import import_module
            release = import_module("migrations.versions.0031_catalog_taxonomy_v4")
            categories = set(re.findall(r"'([a-z_]+)'", definition or ""))
            if categories != set(release.V3_CATEGORIES):
                raise RuntimeError("Catalog category CHECK differs from released 0030")
            running = connection.scalar(text("""
                SELECT COUNT(*) FROM pg_stat_activity
                WHERE datname=current_database() AND pid<>pg_backend_pid()
                  AND state='active'
            """))
            if running:
                raise RuntimeError("Concurrent active PostgreSQL sessions detected")
    finally:
        engine.dispose()


def _backup_verified(path: Path, expected_sha256: str) -> None:
    canonical = (_ROOT / "artifacts").resolve()
    if path.resolve().parent != canonical or path.is_symlink() or not path.is_file():
        raise RuntimeError("Backup must be a regular existing file in local artifacts")
    if path.stat().st_size < 100_000:
        raise RuntimeError("Backup is unexpectedly small")
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise ValueError("Expected SHA256 must be a lowercase digest")
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        if stream.read(5) != b"PGDMP":
            raise RuntimeError("Backup is not a PostgreSQL custom archive")
        stream.seek(0)
        while chunk := stream.read(1 << 20):
            hasher.update(chunk)
    if hasher.hexdigest() != expected_sha256:
        raise RuntimeError("Backup SHA256 mismatch")


def _verify_final() -> None:
    settings = dotenv_values(_ROOT / ".env")
    engine = create_engine(
        URL.create(
            "postgresql+psycopg", username="kpdesk",
            password=settings.get("KP_DESK_DB_PASSWORD"),
            host="127.0.0.1",
            port=int(settings.get("KP_DESK_DB_PORT") or 55432),
            database="kpdesk",
        ), connect_args={"options": "-c default_transaction_read_only=on"},
    )
    try:
        with engine.connect() as connection:
            version = connection.scalar(text(
                "SELECT version_num FROM public.alembic_version"
            ))
            definition = connection.scalar(text("""
                SELECT pg_get_constraintdef(oid) FROM pg_constraint
                WHERE conrelid='public.catalog_items'::regclass
                  AND conname='valid_catalog_category' AND contype='c'
            """))
            from importlib import import_module
            release = import_module("migrations.versions.0031_catalog_taxonomy_v4")
            if version != _NEW or (
                set(re.findall(r"'([a-z_]+)'", definition or ""))
                != set(release.V4_CATEGORIES)
            ):
                raise RuntimeError("Post-migration verification failed")
            print(f"LOCAL_KPDESK_MIGRATION_OK version={version} categories=40")
    finally:
        engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--sha256", type=str, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    _backup_verified(args.backup, args.sha256)
    _read_only_preflight()
    print("LOCAL_KPDESK_0031_PREFLIGHT_PASS source=0030 categories=34")
    if not args.apply:
        return 0
    config = Config(str(_BACKEND / "alembic.ini"))
    # The promoted migration has its own explicit opt-in and strict DB checks.
    import os
    original = os.environ.get("KP_DESK_APPROVE_LOCAL_0031")
    try:
        os.environ["KP_DESK_APPROVE_LOCAL_0031"] = "1"
        command.upgrade(config, "live_release@head")
    finally:
        if original is None:
            os.environ.pop("KP_DESK_APPROVE_LOCAL_0031", None)
        else:
            os.environ["KP_DESK_APPROVE_LOCAL_0031"] = original
    _verify_final()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
