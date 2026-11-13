"""A86 clone-only Alembic rehearsal of reviewed supplier checkpoint storage.

Revision ID: 0031_reviewed_catalog_clone
Revises: 0030_tenant_llm_cache

NOT authorized for production. Both directions reject unmarked databases.
The DDL installers are shared with the clone integration tests; they enforce
their own provenance, owner and exact-base-revision checks.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone
from kpdesk.db.review_clone_schema.checkpoint import install_checkpoint_prototype
from kpdesk.db.review_clone_schema.current import install_current_review_index
from kpdesk.db.review_clone_schema.paged import install_paged_checkpoint_recovery
from kpdesk.db.review_clone_schema.probe import install_checkpoint_probe
from kpdesk.db.review_clone_schema.publication import install_publication_provenance
from kpdesk.db.review_schema_manifest import REVIEW_FUNCTIONS as _FUNCTIONS
from kpdesk.db.review_schema_manifest import REVIEW_TABLES as _TABLES

revision = "0031_reviewed_catalog_clone"
down_revision = "0030_tenant_llm_cache"
branch_labels = None
depends_on = None


def _assert_target(expected_revision: str) -> sa.engine.Connection:
    connection = op.get_bind()
    if (
        not connection.in_transaction()
        or not is_live_accounts_http_clone(connection)
        or connection.scalar(sa.text("SELECT current_user")) != "kpdesk"
        or connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        != expected_revision
    ):
        raise RuntimeError("Reviewed catalog Alembic revision is CLONE ONLY.")
    return connection


def upgrade() -> None:
    connection = _assert_target("0030_tenant_llm_cache")
    if any(connection.scalar(sa.text(
        "SELECT to_regclass(:object_name)"
    ), {"object_name": "public." + table}) is not None for table in _TABLES):
        raise RuntimeError("Reviewed catalog clone migration objects already exist.")
    if connection.scalar(sa.text("""
        SELECT count(*) FROM pg_extension WHERE extname='pgcrypto'
    """)) != 0:
        raise RuntimeError("Clone requires no preexisting pgcrypto extension.")
    install_checkpoint_prototype(connection)
    install_current_review_index(connection)
    install_paged_checkpoint_recovery(connection)
    install_checkpoint_probe(connection)
    install_publication_provenance(connection)


def downgrade() -> None:
    connection = _assert_target("0031_reviewed_catalog_clone")
    # No silent data loss. A rollback with human reviews/provenance is refused,
    # even in disposable clones; backup and a separate data-aware plan required.
    for table in _TABLES:
        if table == "a84_provenance_signing_secret":
            continue
        count = connection.scalar(sa.text(
            "SELECT count(*) FROM public." + table
        ))
        if count != 0:
            raise RuntimeError("Refusing downgrade of nonempty reviewed catalog history.")
    connection.execute(sa.text("""
        DROP TRIGGER a77_materialize_decisions
        ON public.a72_canonical_row_decisions
    """))
    for signature in _FUNCTIONS:
        connection.execute(sa.text("DROP FUNCTION public." + signature + " RESTRICT"))
    for table in _TABLES:
        connection.execute(sa.text("DROP TABLE public." + table + " RESTRICT"))
    connection.execute(sa.text("DROP EXTENSION IF EXISTS pgcrypto RESTRICT"))
