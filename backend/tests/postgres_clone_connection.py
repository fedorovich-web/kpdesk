"""Shared PostgreSQL connection construction for opt-in isolated clone tests."""
from __future__ import annotations

import os
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from dotenv import dotenv_values
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import URL, Engine

_ROOT = Path(__file__).resolve().parents[2]
LEGACY_TEST_OPT_IN = "KP_DESK_RUN_ISOLATED_LEGACY_PG"
_LEGACY_REVISION = "0005_tenant_expand"


def postgres_test_engine(name: str, *, autocommit: bool = False) -> Engine:
    settings = dotenv_values(_ROOT / ".env")
    password = settings.get("KP_DESK_DB_PASSWORD")
    if not isinstance(password, str) or len(password) < 32:
        raise RuntimeError("Isolated PostgreSQL tests require local owner credentials.")
    return create_engine(
        URL.create(
            "postgresql+psycopg",
            username="kpdesk",
            password=password,
            host="127.0.0.1",
            port=int(settings.get("KP_DESK_DB_PORT") or 55432),
            database=name,
        ),
        isolation_level="AUTOCOMMIT" if autocommit else None,
    )


def _clone_marker(name: str) -> str:
    if re.fullmatch(r"kpdesk_accounts_http_[0-9a-f]{12}", name) is None:
        raise RuntimeError("Legacy SQL tests require a generated clone name.")
    return "kpdesk:accounts-http:" + name.rsplit("_", 1)[-1]


def require_legacy_test_clone(connection: Connection, name: str) -> None:
    marker = _clone_marker(name)
    identity = connection.execute(text("""
        SELECT current_database(), current_user, pg_get_userbyid(datdba),
               shobj_description(oid, 'pg_database')
        FROM pg_database WHERE datname = current_database()
    """)).one()
    if tuple(identity) != (name, "kpdesk", "kpdesk", marker):
        raise RuntimeError("Legacy SQL clone identity, owner or marker mismatch.")


def drop_legacy_test_clone(admin: Engine, name: str) -> None:
    marker = _clone_marker(name)
    with admin.connect() as connection:
        identity = connection.execute(text("""
            SELECT current_database(), current_user, pg_get_userbyid(datdba),
                   shobj_description(oid, 'pg_database')
            FROM pg_database WHERE datname = :name
        """), {"name": name}).one()
        if tuple(identity) != ("postgres", "kpdesk", "kpdesk", marker):
            raise RuntimeError("Refusing to drop an unidentified legacy SQL clone.")
        connection.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')


def _migrate_legacy_database(connection: Connection) -> None:
    config = Config(str(_ROOT / "backend/alembic.ini"))
    config.attributes["connection"] = connection
    command.upgrade(config, _LEGACY_REVISION)


@contextmanager
def legacy_test_database() -> Iterator[Engine]:
    """Fresh pre-quarantine schema per test; never reset an existing database."""
    if os.environ.get(LEGACY_TEST_OPT_IN) != "1":
        raise RuntimeError("Legacy SQL clone creation requires explicit opt-in.")
    name = "kpdesk_accounts_http_" + secrets.token_hex(6)
    marker = _clone_marker(name)
    admin = postgres_test_engine("postgres", autocommit=True)
    engine: Engine | None = None
    created = False
    try:
        with admin.connect() as connection:
            if tuple(connection.execute(text("SELECT current_database(), current_user")).one()) != (
                "postgres", "kpdesk",
            ):
                raise RuntimeError("Legacy clone provisioning requires the local admin database.")
            connection.exec_driver_sql(f'CREATE DATABASE "{name}" TEMPLATE template0 OWNER kpdesk')
            created = True
            connection.exec_driver_sql(f"COMMENT ON DATABASE \"{name}\" IS '{marker}'")
        engine = postgres_test_engine(name)
        with engine.begin() as connection:
            require_legacy_test_clone(connection, name)
            _migrate_legacy_database(connection)
            require_legacy_test_clone(connection, name)
        yield engine
    finally:
        try:
            if engine is not None:
                engine.dispose()
            if created:
                drop_legacy_test_clone(admin, name)
        finally:
            admin.dispose()
