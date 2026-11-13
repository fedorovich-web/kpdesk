"""Gated tenant supplier registry, isolated from immutable quote provenance.

Revision ID: 0036_supplier_registry
Revises: 0035_supplier_source_discovery

Editable directory != reviewed price. No runtime table grants or automatic
production activation; schema object SQL is shared with the tagged-clone E2E.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone
from kpdesk.db.supplier_registry_schema import create_supplier_directory_objects

revision = "0036_supplier_registry"
down_revision = "0035_supplier_source_discovery"
branch_labels = None
depends_on = None

_APPROVAL = "kpdesk_supplier_registry_0036_approved"
_LIST = "public.kpdesk_supplier_registry_list(uuid,integer,integer)"
_UPSERT = "public.kpdesk_supplier_registry_upsert(uuid,text,text,text,text,integer)"
_STATUS = "public.kpdesk_supplier_registry_status(uuid,text)"


def _preflight(expected_revision: str) -> sa.engine.Connection:
    config = op.get_context().config
    if config is None or config.attributes.get(_APPROVAL) is not True:
        raise RuntimeError("Supplier registry requires explicit Alembic approval.")
    connection = op.get_bind()
    if not connection.in_transaction():
        raise RuntimeError("Supplier registry migration requires transaction.")
    database, role, previous = connection.execute(sa.text("""
        SELECT current_database(),current_user,
               (SELECT version_num FROM public.alembic_version)
    """)).one()
    if (
        role != "kpdesk" or previous != expected_revision
        or (database != "kpdesk" and not is_live_accounts_http_clone(connection))
    ):
        raise RuntimeError("Supplier registry migration target is unsafe.")
    connection.execute(sa.text("SET LOCAL lock_timeout='5s'"))
    return connection


def upgrade() -> None:
    connection = _preflight(down_revision)
    create_supplier_directory_objects(connection)


def downgrade() -> None:
    connection = _preflight(revision)
    if connection.scalar(sa.text("""
        SELECT count(*) FROM public.tenant_supplier_registry
    """)) != 0:
        raise RuntimeError("Supplier registry contains business data; refusing downgrade.")
    connection.execute(sa.text(f"DROP FUNCTION {_STATUS} RESTRICT"))
    connection.execute(sa.text(f"DROP FUNCTION {_UPSERT} RESTRICT"))
    connection.execute(sa.text(f"DROP FUNCTION {_LIST} RESTRICT"))
    connection.execute(sa.text("DROP TABLE public.tenant_supplier_registry RESTRICT"))
