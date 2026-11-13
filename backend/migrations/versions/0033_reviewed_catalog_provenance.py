"""Install inactive, tenant-isolated reviewed-catalog provenance schema.

Revision ID: 0033_reviewed_catalog_provenance
Revises: 0032_supplier_quote_audit

The existing clone-tested DDL is reused, not reimplemented. Installation does
not enable HTTP publication, provision a signing key, or grant runtime execute.
An explicit Alembic approval attribute and verified DB/role/revision are
required; ordinary upgrade commands must fail closed.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone
from kpdesk.db.review_clone_schema.checkpoint import _install_checkpoint_objects
from kpdesk.db.review_clone_schema.current import _install_current_review_objects
from kpdesk.db.review_clone_schema.paged import _install_paged_checkpoint_objects
from kpdesk.db.review_clone_schema.probe import _install_checkpoint_probe_objects
from kpdesk.db.review_clone_schema.publication import _install_publication_objects
from kpdesk.db.review_schema_manifest import REVIEW_FUNCTIONS, REVIEW_TABLES

revision = "0033_reviewed_catalog_provenance"
down_revision = "0032_supplier_quote_audit"
branch_labels = None
depends_on = None

_APPROVAL = "kpdesk_review_schema_0033_approved"


def _preflight(expected_revision: str) -> sa.engine.Connection:
    config = op.get_context().config
    if config is None or config.attributes.get(_APPROVAL) is not True:
        raise RuntimeError("Reviewed-catalog schema requires explicit Alembic approval.")
    connection = op.get_bind()
    if not connection.in_transaction():
        raise RuntimeError("Reviewed-catalog migration requires a transaction.")
    database, role, current_revision = connection.execute(
        sa.text("""
        SELECT current_database(),current_user,
               (SELECT version_num FROM public.alembic_version)
    """)
    ).one()
    if (
        role != "kpdesk"
        or current_revision != expected_revision
        or (database != "kpdesk" and not is_live_accounts_http_clone(connection))
    ):
        raise RuntimeError("Reviewed-catalog migration target is unsafe.")
    connection.execute(sa.text("SET LOCAL lock_timeout='5s'"))
    return connection


def _check_inactive(connection: sa.engine.Connection) -> None:
    for table in REVIEW_TABLES:
        if (
            connection.scalar(sa.text("SELECT to_regclass(:name)"), {"name": "public." + table})
            is not None
        ):
            raise RuntimeError("Reviewed-catalog schema already exists.")
    names = [signature.split("(", 1)[0] for signature in REVIEW_FUNCTIONS]
    if (
        connection.scalar(
            sa.text("""
        SELECT count(*) FROM pg_catalog.pg_proc p
        JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='public' AND p.proname=ANY(:names)
    """),
            {"names": names},
        )
        != 0
    ):
        raise RuntimeError("Reviewed-catalog functions already exist.")


def upgrade() -> None:
    connection = _preflight(down_revision)
    namespace = connection.scalar(
        sa.text("""
        SELECT n.nspname FROM pg_catalog.pg_extension e
        JOIN pg_catalog.pg_namespace n ON n.oid=e.extnamespace
        WHERE e.extname='pgcrypto'
    """)
    )
    if namespace != "public":
        raise RuntimeError("DBA-managed pgcrypto in public is required.")
    _check_inactive(connection)
    _install_checkpoint_objects(connection)
    _install_current_review_objects(connection)
    _install_paged_checkpoint_objects(connection)
    _install_checkpoint_probe_objects(connection)
    # No key can be used to sign, and the HTTP route remains disabled.
    _install_publication_objects(connection, signing_secret=None)
    # Clone installers grant EXECUTE for rehearsal. The real migration
    # explicitly revokes it before any transaction can commit.
    for signature in REVIEW_FUNCTIONS:
        connection.execute(
            sa.text(
                f"REVOKE ALL ON FUNCTION public.{signature} "
                "FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime"
            )
        )


def downgrade() -> None:
    connection = _preflight(revision)
    for table in REVIEW_TABLES:
        if connection.scalar(sa.text(f"SELECT count(*) FROM public.{table}")) != 0:
            raise RuntimeError("Reviewed-catalog history is nonempty; refusing downgrade.")
    for signature in REVIEW_FUNCTIONS:
        permitted = connection.scalar(
            sa.text("""
            SELECT has_function_privilege(
              'kpdesk_tenant_runtime',:signature,'EXECUTE'
            )
        """),
            {"signature": "public." + signature},
        )
        if permitted is not False:
            raise RuntimeError("Deactivate reviewed-catalog API before downgrade.")
    connection.execute(
        sa.text("""
        DROP TRIGGER a77_materialize_decisions
        ON public.a72_canonical_row_decisions
    """)
    )
    for signature in REVIEW_FUNCTIONS:
        connection.execute(sa.text(f"DROP FUNCTION public.{signature} RESTRICT"))
    for table in REVIEW_TABLES:
        connection.execute(sa.text(f"DROP TABLE public.{table} RESTRICT"))
    # Never drop pgcrypto: it can be shared with other application objects.
