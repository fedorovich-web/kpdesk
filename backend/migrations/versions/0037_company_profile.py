"""Tenant company facts and generation preferences; explicit rollout approval."""
import sqlalchemy as sa
from alembic import op

from kpdesk.db.company_profile_schema import (
    READ_SIGNATURE,
    SAVE_SIGNATURE,
    create_company_profile_objects,
)
from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone

revision = "0037_company_profile"
down_revision = "0036_supplier_registry"
branch_labels = None
depends_on = None


def _preflight(expected: str) -> sa.engine.Connection:
    config = op.get_context().config
    if config is None or config.attributes.get("kpdesk_company_profile_0037_approved") is not True:
        raise RuntimeError("Company profile migration requires explicit approval")
    connection = op.get_bind()
    database, role, previous = connection.execute(sa.text("""
        SELECT current_database(),current_user,(SELECT version_num FROM public.alembic_version)
    """)).one()
    if (not connection.in_transaction() or role != "kpdesk" or previous != expected
        or (database != "kpdesk" and not is_live_accounts_http_clone(connection))):
        raise RuntimeError("Unsafe company profile migration target")
    connection.execute(sa.text("SET LOCAL lock_timeout='5s'"))
    return connection


def upgrade() -> None:
    create_company_profile_objects(_preflight(down_revision))


def downgrade() -> None:
    connection = _preflight(revision)
    if connection.scalar(sa.text("SELECT count(*) FROM public.organization_proposal_profiles")) != 0:
        raise RuntimeError("Company profiles contain business data; refusing downgrade")
    for signature in (SAVE_SIGNATURE, READ_SIGNATURE):
        connection.execute(sa.text(f"DROP FUNCTION {signature} RESTRICT"))
    connection.execute(sa.text("DROP TABLE public.organization_proposal_profiles RESTRICT"))
