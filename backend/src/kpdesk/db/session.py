"""Read-only configuration of PostgreSQL connections under kpdesk_app only."""

from functools import lru_cache

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from kpdesk.config import application_database_url


@lru_cache(maxsize=1)
def app_engine() -> Engine:
    return create_engine(
        application_database_url(),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=0,
        connect_args={"connect_timeout": 3},
    )


def assert_unprivileged_session(session: Session) -> None:
    result = session.execute(
        text(
            "SELECT current_database(), current_user, "
            "(SELECT rolsuper FROM pg_roles WHERE rolname = current_user), "
            "EXISTS (SELECT 1 FROM pg_attribute "
            "WHERE attrelid = 'public.catalog_active'::regclass "
            "AND attname = 'id' AND NOT attisdropped)"
        )
    ).one()
    if (
        result[0] != "kpdesk" or result[1] != "kpdesk_app"
        or result[2] is not False or result[3] is not True
    ):
        raise RuntimeError("Refusing elevated or unexpected PostgreSQL session.")
