"""A93 schema-only production migration CANDIDATE; not in default Alembic graph.

Revision ID: 0031_review_schema_candidate
Revises: 0030_tenant_llm_cache

Requires an explicitly authorized programmatic Alembic invocation and a
pre-provisioned pgcrypto extension in public. No HMAC key is generated or
persisted by this migration. No production HTTP route is enabled.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from kpdesk.db.review_clone_schema.checkpoint import _install_checkpoint_objects
from kpdesk.db.review_clone_schema.current import _install_current_review_objects
from kpdesk.db.review_clone_schema.paged import _install_paged_checkpoint_objects
from kpdesk.db.review_clone_schema.probe import _install_checkpoint_probe_objects
from kpdesk.db.review_clone_schema.publication import _install_publication_objects
from kpdesk.db.review_schema_manifest import REVIEW_FUNCTIONS, REVIEW_TABLES

revision = "0031_review_schema_candidate"
down_revision = "0030_tenant_llm_cache"
branch_labels = None
depends_on = None

_BASE = "0030_tenant_llm_cache"
_APPROVAL = "a93_schema_candidate_approved"


def _assert_authorized(expected_revision: str) -> sa.engine.Connection:
    """Fail before any DDL unless approval, owner, transaction and base match."""
    config = op.get_context().config
    if config is None or config.attributes.get(_APPROVAL) is not True:
        raise RuntimeError("A93 schema-only candidate requires explicit approval.")
    connection = op.get_bind()
    if (
        not connection.in_transaction()
        or connection.scalar(sa.text("SELECT current_user")) != "kpdesk"
        or connection.scalar(sa.text(
            "SELECT version_num FROM public.alembic_version"
        )) != expected_revision
    ):
        raise RuntimeError("A93 schema-only candidate target is unsafe.")
    return connection


def upgrade() -> None:
    connection = _assert_authorized(_BASE)
    extension_namespace = connection.scalar(sa.text("""
        SELECT n.nspname FROM pg_catalog.pg_extension e
        JOIN pg_catalog.pg_namespace n ON n.oid=e.extnamespace
        WHERE e.extname='pgcrypto'
    """))
    if extension_namespace != "public":
        raise RuntimeError(
            "A93 requires DBA-managed pgcrypto in public; it will not install it."
        )
    for table in REVIEW_TABLES:
        if connection.scalar(sa.text(
            "SELECT to_regclass(:relation)"
        ), {"relation": "public." + table}) is not None:
            raise RuntimeError("A93 reviewed-catalog SQL objects already exist.")
    function_names = [name.split("(", 1)[0] for name in REVIEW_FUNCTIONS]
    if connection.scalar(sa.text("""
        SELECT count(*) FROM pg_catalog.pg_proc p
        JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='public' AND p.proname=ANY(:names)
    """), {"names": function_names}) != 0:
        raise RuntimeError("A93 reviewed-catalog functions already exist.")

    # Same SQL implementations as the clone proofs, no parallel DDL copy.
    _install_checkpoint_objects(connection)
    _install_current_review_objects(connection)
    _install_paged_checkpoint_objects(connection)
    _install_checkpoint_probe_objects(connection)
    _install_publication_objects(connection, signing_secret=None)

    # Schema installation is NOT activation. No runtime may invoke even the
    # checked SECURITY DEFINER functions until separate authorized rollout.
    for signature in REVIEW_FUNCTIONS:
        connection.execute(sa.text(
            f"REVOKE ALL ON FUNCTION public.{signature} "
            "FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime"
        ))


def downgrade() -> None:
    connection = _assert_authorized(revision)
    # No data-aware rollback here. Reject keys, human decisions and provenance.
    for table in REVIEW_TABLES:
        if connection.scalar(sa.text(
            "SELECT count(*) FROM public." + table
        )) != 0:
            raise RuntimeError("A93 refuses destructive rollback of nonempty data.")
    # Deactivation must precede downgrade even if all tables are empty.
    if connection.scalar(sa.text("""
        SELECT has_function_privilege(
          'kpdesk_tenant_runtime',
          'public.a79_review_snapshot(uuid,text,text,text,text,text,uuid)',
          'EXECUTE'
        )
    """)) is not False:
        raise RuntimeError("A93 refuses rollback while review functions are active.")
    connection.execute(sa.text("""
        DROP TRIGGER a77_materialize_decisions
        ON public.a72_canonical_row_decisions
    """))
    for signature in REVIEW_FUNCTIONS:
        connection.execute(sa.text(
            f"DROP FUNCTION public.{signature} RESTRICT"
        ))
    for table in REVIEW_TABLES:
        connection.execute(sa.text(f"DROP TABLE public.{table} RESTRICT"))
    # pgcrypto is shared/DBA-managed: it is NEVER dropped here.
