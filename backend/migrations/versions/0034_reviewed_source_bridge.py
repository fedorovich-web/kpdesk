"""Tenant-authorized, read-only verification of reviewed canonical catalog sources.

Revision ID: 0034_reviewed_source_bridge
Revises: 0033_reviewed_catalog_provenance

A successful bridge means the immutable signed publication record matches the
catalog digest and original supplier source. It never activates checkpoint
writers, provisions a signer, or permits access to the underlying review tables.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone
from kpdesk.db.review_source_readers import BRIDGE_DDL, BRIDGE_SIGNATURE

revision = "0034_reviewed_source_bridge"
down_revision = "0033_reviewed_catalog_provenance"
branch_labels = None
depends_on = None

_APPROVAL = "kpdesk_review_bridge_0034_approved"
_SIGNATURE = BRIDGE_SIGNATURE


def _preflight(expected_revision: str) -> sa.engine.Connection:
    context = op.get_context()
    if context.config is None or context.config.attributes.get(_APPROVAL) is not True:
        raise RuntimeError("Reviewed source bridge requires explicit Alembic approval.")
    connection = op.get_bind()
    if not connection.in_transaction():
        raise RuntimeError("Reviewed source bridge requires a transaction.")
    database, role, current = connection.execute(sa.text("""
        SELECT current_database(),current_user,
               (SELECT version_num FROM public.alembic_version)
    """)).one()
    if (
        role != "kpdesk" or current != expected_revision
        or (database != "kpdesk" and not is_live_accounts_http_clone(connection))
    ):
        raise RuntimeError("Reviewed source bridge migration target is unsafe.")
    connection.execute(sa.text("SET LOCAL lock_timeout='5s'"))
    return connection


def upgrade() -> None:
    connection = _preflight(down_revision)
    if connection.scalar(sa.text("SELECT to_regprocedure(:signature)"), {
        "signature": _SIGNATURE,
    }) is not None:
        raise RuntimeError("Reviewed source bridge already exists.")
    # The SECURITY DEFINER owner reads a79, but the tenant role is allowed to
    # call only this boolean function. Original and canonical digests must match
    # the signed publication; unreviewed profile metadata alone never suffices.
    connection.execute(sa.text(BRIDGE_DDL))
    connection.execute(sa.text(f"""
        REVOKE ALL ON FUNCTION {_SIGNATURE}
        FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime
    """))
    connection.execute(sa.text(f"""
        GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO kpdesk_tenant_runtime
    """))


def downgrade() -> None:
    connection = _preflight(revision)
    # Dropping this read-only entrypoint never removes existing provenance.
    connection.execute(sa.text(f"DROP FUNCTION {_SIGNATURE} RESTRICT"))
