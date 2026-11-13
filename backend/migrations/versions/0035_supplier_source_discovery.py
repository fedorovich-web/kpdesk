"""Read-only indexed discovery of signed reviewed supplier publications.

Revision ID: 0035_supplier_source_discovery
Revises: 0034_reviewed_source_bridge

This only exposes bounded provenance *identifiers* to authenticated tenant
members. Raw decisions, reviewer identities and HMAC keys remain inaccessible.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone
from kpdesk.db.review_source_readers import (
    DISCOVERY_DDL,
    DISCOVERY_INDEX,
    DISCOVERY_SIGNATURE,
)

revision = "0035_supplier_source_discovery"
down_revision = "0034_reviewed_source_bridge"
branch_labels = None
depends_on = None

_APPROVAL = "kpdesk_supplier_discovery_0035_approved"
_SIGNATURE = DISCOVERY_SIGNATURE
_INDEX = DISCOVERY_INDEX


def _preflight(expected_revision: str) -> sa.engine.Connection:
    config = op.get_context().config
    if config is None or config.attributes.get(_APPROVAL) is not True:
        raise RuntimeError("Supplier discovery requires explicit Alembic approval.")
    connection = op.get_bind()
    if not connection.in_transaction():
        raise RuntimeError("Supplier discovery requires a transaction.")
    database, role, version = connection.execute(sa.text("""
        SELECT current_database(),current_user,
               (SELECT version_num FROM public.alembic_version)
    """)).one()
    if (
        role != "kpdesk" or version != expected_revision
        or (database != "kpdesk" and not is_live_accounts_http_clone(connection))
    ):
        raise RuntimeError("Supplier discovery migration target is unsafe.")
    connection.execute(sa.text("SET LOCAL lock_timeout='5s'"))
    return connection


def upgrade() -> None:
    connection = _preflight(down_revision)
    if connection.scalar(sa.text("SELECT to_regprocedure(:signature)"), {
        "signature": _SIGNATURE,
    }) is not None:
        raise RuntimeError("Supplier discovery function already exists.")
    connection.execute(sa.text(f"""
        CREATE INDEX {_INDEX} ON public.a79_catalog_review_provenance
          (organization_id,supplier_id,created_at DESC,catalog_version_id DESC)
    """))
    connection.execute(sa.text(DISCOVERY_DDL))
    connection.execute(sa.text(f"""
        REVOKE ALL ON FUNCTION {_SIGNATURE}
          FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime
    """))
    connection.execute(sa.text(f"""
        GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO kpdesk_tenant_runtime
    """))


def downgrade() -> None:
    connection = _preflight(revision)
    connection.execute(sa.text(f"DROP FUNCTION {_SIGNATURE} RESTRICT"))
    connection.execute(sa.text(f"DROP INDEX public.{_INDEX}"))
