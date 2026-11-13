"""Dedicated physical PostgreSQL connections for restricted live auth/tenant API.

Secrets never come from browser, query strings or legacy kpdesk_app credentials.
The existing application remains on its current database engine by default.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool

from kpdesk.config import PROJECT_ROOT

_SECRETS = PROJECT_ROOT / "credentials" / "kpdesk-runtime-db.json"
_ACCOUNTS = (
    ("kpdesk_auth_service", "kpdesk_auth_runtime"),
    ("kpdesk_tenant_service", "kpdesk_tenant_runtime"),
)


def _restricted_engine(
    username: str, role: str, password: str, port: int,
    *, database: str = "kpdesk", host: str = "127.0.0.1",
) -> Engine:
    if database != "kpdesk":
        import re

        if re.fullmatch(r"kpdesk_accounts_http_[0-9a-f]{12}", database) is None:
            raise ValueError("Restricted connection cannot access an unmarked database.")
    engine = create_engine(
        URL.create(
            "postgresql+psycopg", username=username, password=password,
            host=host, port=port, database=database,
        ),
        poolclass=NullPool, pool_pre_ping=True,
        connect_args={"connect_timeout": 4},
    )

    @event.listens_for(engine, "begin")
    def _assume_runtime_role(connection: Connection) -> None:
        # LOCAL means PostgreSQL automatically clears the role after txn end.
        # NullPool additionally guarantees independent physical connections.
        connection.exec_driver_sql("SET LOCAL ROLE " + role)

    return engine


@lru_cache(maxsize=1)
def live_service_engines() -> tuple[Engine, Engine]:
    from dotenv import dotenv_values

    config = dotenv_values(PROJECT_ROOT / ".env")
    secret_location = os.environ.get("KP_DESK_SERVICE_CREDENTIALS_FILE")
    if secret_location and not Path(secret_location).is_absolute():
        raise RuntimeError("The service credentials file path must be absolute.")
    secret_file = Path(secret_location) if secret_location else _SECRETS
    if not secret_file.is_file():
        raise RuntimeError("Dedicated PostgreSQL service credentials unavailable.")
    port = int(os.environ.get("KP_DESK_DB_PORT") or config.get("KP_DESK_DB_PORT") or 55432)
    if not 1 <= port <= 65535:
        raise RuntimeError("Invalid PostgreSQL port.")
    host = os.environ.get("KP_DESK_DB_HOST") or "127.0.0.1"
    credentials = json.loads(secret_file.read_text(encoding="utf-8"))
    if not isinstance(credentials, dict):
        raise TypeError("Unexpected service credential file format.")
    engines: list[Engine] = []
    try:
        for username, role in _ACCOUNTS:
            value = credentials.get(username)
            if not isinstance(value, str) or len(value) < 48:
                raise RuntimeError("Incomplete separated PostgreSQL credentials.")
            engine = _restricted_engine(username, role, value, port, host=host)
            # Dispose even the current engine if its identity/RLS preflight fails.
            engines.append(engine)
            with engine.begin() as connection:
                actual = connection.execute(text("""
                    SELECT session_user, current_user, current_database()
                """)).one()
                if actual != (username, role, "kpdesk"):
                    raise RuntimeError("PostgreSQL service role identity mismatch.")
                if connection.scalar(text("""
                    SELECT to_regprocedure(
                        'public.kpdesk_session_tenant_allowed(uuid,text)'
                    ) IS NOT NULL
                """)) is not True:
                    raise RuntimeError("Session-bound RLS is missing.")
    except Exception:
        for engine in engines:
            engine.dispose()
        raise
    return engines[0], engines[1]
