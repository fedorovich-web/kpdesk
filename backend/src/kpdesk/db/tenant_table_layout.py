"""Explicit storage layout for live release vs isolated A8 tenant tests.

Table names are compile-time literals, not HTTP-controlled identifiers.
The live layout is only available inside the restricted tenant PostgreSQL role.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Connection, text

from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone


@dataclass(frozen=True, slots=True)
class TenantTableLayout:
    active_catalog: str
    catalog_idempotency: str
    quote_idempotency: str


LIVE_TABLES = TenantTableLayout(
    active_catalog="tenant_catalog_active",
    catalog_idempotency="tenant_catalog_idempotency",
    quote_idempotency="tenant_quote_write_requests",
)
ISOLATED_TEST_TABLES = TenantTableLayout(
    active_catalog="catalog_active",
    catalog_idempotency="idempotency_records",
    quote_idempotency="quote_write_requests",
)


def tenant_table_layout(connection: Connection) -> TenantTableLayout:
    database, role = connection.execute(
        text("SELECT current_database(), current_user")
    ).one()
    if database == "kpdesk" or is_live_accounts_http_clone(connection):
        if role != "kpdesk_tenant_runtime":
            raise RuntimeError(
                "Live tenant tables require the isolated tenant PostgreSQL role."
            )
        return LIVE_TABLES
    if role != "kpdesk_app":
        raise RuntimeError("Unrecognized test tenant database role.")
    return ISOLATED_TEST_TABLES
