"""Alembic migration runs only against an explicitly selected local DB."""

from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import dotenv_values
from sqlalchemy import create_engine, pool, text
from sqlalchemy.engine import URL

from kpdesk.db import models as catalog_models  # noqa: F401
from kpdesk.db import tenant_models  # noqa: F401
from kpdesk.db.base import Base

config = context.config
if config.config_file_name and config.attributes.get("connection") is None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
root = Path(__file__).resolve().parents[2]
values = dotenv_values(root / ".env")
password = values.get("KP_DESK_DB_PASSWORD")
if not isinstance(password, str) or len(password) < 32:
    raise RuntimeError("Local .env database password is missing")
db_name = os.environ.get("KP_DESK_DB_NAME", "kpdesk")
if db_name not in ("kpdesk", "kpdesk_test", "kpdesk_tenant_test"):
    raise RuntimeError("Only the named local KPDesk databases are allowed")
db_url = URL.create(
    "postgresql+psycopg",
    username="kpdesk",
    password=password,
    host="127.0.0.1",
    port=int(values.get("KP_DESK_DB_PORT") or "55432"),
    database=db_name,
)


def run_migrations_offline() -> None:
    context.configure(
        url=db_url.render_as_string(hide_password=False),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Explicit programmatic injection is used exclusively by marked disposable
    # clones. The ordinary CLI retains the existing strict DB-name allowlist.
    injected = config.attributes.get("connection")
    if injected is not None:
        from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone

        if (
            not is_live_accounts_http_clone(injected)
            or injected.scalar(text("SELECT current_user")) != "kpdesk"
        ):
            raise RuntimeError("Injected Alembic connection requires clone owner.")
        context.configure(connection=injected, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = create_engine(db_url, poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
