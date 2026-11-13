"""Structured sender details in company profiles; explicit rollout approval."""
import sqlalchemy as sa
from alembic import op

from kpdesk.db.company_profile_schema import SAVE as SAVE_V1
from kpdesk.db.issuer_requisites_schema import SAVE
from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone

revision = "0038_issuer_requisites"
down_revision = "0037_company_profile"
branch_labels = None
depends_on = None


def _preflight(expected: str) -> sa.engine.Connection:
    config = op.get_context().config
    if config is None or config.attributes.get("kpdesk_issuer_requisites_0038_approved") is not True:
        raise RuntimeError("Sender requisites migration requires explicit approval")
    connection = op.get_bind()
    database, role, previous = connection.execute(sa.text("""
        SELECT current_database(),current_user,(SELECT version_num FROM public.alembic_version)
    """)).one()
    if (not connection.in_transaction() or role != "kpdesk" or previous != expected
        or (database != "kpdesk" and not is_live_accounts_http_clone(connection))):
        raise RuntimeError("Unsafe sender requisites migration target")
    connection.execute(sa.text("SET LOCAL lock_timeout='5s'"))
    return connection


def upgrade() -> None:
    _preflight(down_revision).execute(sa.text(SAVE))


def downgrade() -> None:
    connection = _preflight(revision)
    if connection.scalar(sa.text("""
        SELECT count(*) FROM public.organization_proposal_profiles WHERE profile ? 'issuer_details'
    """)) != 0:
        raise RuntimeError("Profiles contain sender requisites; refusing lossy downgrade")
    connection.execute(sa.text(SAVE_V1.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)))
