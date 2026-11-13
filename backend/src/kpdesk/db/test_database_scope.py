"""Strict allowlist for temporary, marked PostgreSQL auth integration clones.

A per-run clone must have both an exact generated name and a database-owner
COMMENT containing that same nonce. The comment is a DBA-controlled marker,
not a user-controlled session GUC. Production kpdesk can never satisfy it.
"""
from __future__ import annotations

import re

from sqlalchemy import Connection, text

_CLONE = re.compile(r"kpdesk_auth_e2e_([0-9a-f]{12})\Z")


def is_auth_test_database(connection: Connection) -> bool:
    database = connection.scalar(text("SELECT current_database()"))
    if database == "kpdesk_tenant_test":
        return True
    if not isinstance(database, str):
        return False
    match = _CLONE.fullmatch(database)
    if match is None:
        return False
    marker = connection.scalar(text("""
        SELECT shobj_description(oid, 'pg_database')
        FROM pg_database WHERE datname = current_database()
    """))
    return isinstance(marker, str) and marker == (
        f"kpdesk:isolated-auth-e2e:{match.group(1)}"
    )
