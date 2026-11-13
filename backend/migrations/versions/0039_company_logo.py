"""Bounded sender logo snapshots; explicit rollout approval required."""
import sqlalchemy as sa
from alembic import op

from kpdesk.db.company_logo_schema import MAX_PROFILE_BYTES, SAVE, resize_profile_constraint
from kpdesk.db.issuer_requisites_schema import SAVE as SAVE_0038
from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone

revision = "0039_company_logo"
down_revision = "0038_issuer_requisites"
branch_labels = None
depends_on = None


def _preflight(expected: str) -> sa.engine.Connection:
    config = op.get_context().config
    if config is None or config.attributes.get("kpdesk_company_logo_0039_approved") is not True:
        raise RuntimeError("Company logo migration requires explicit approval")
    connection = op.get_bind()
    database, role, previous = connection.execute(sa.text("""
        SELECT current_database(),current_user,(SELECT version_num FROM public.alembic_version)
    """)).one()
    if (not connection.in_transaction() or role != "kpdesk" or previous != expected
        or (database != "kpdesk" and not is_live_accounts_http_clone(connection))):
        raise RuntimeError("Unsafe company logo migration target")
    connection.execute(sa.text("SET LOCAL lock_timeout='5s'"))
    return connection


def upgrade() -> None:
    connection = _preflight(down_revision)
    resize_profile_constraint(connection, MAX_PROFILE_BYTES)
    connection.execute(sa.text(SAVE))


def downgrade() -> None:
    connection = _preflight(revision)
    if connection.scalar(sa.text("""
        SELECT EXISTS(SELECT 1 FROM public.organization_proposal_profiles
                      WHERE profile ? 'logo' OR octet_length(profile::text)>12000)
            OR EXISTS(SELECT 1 FROM public.quote_revisions
                      WHERE snapshot #> '{document,company_profile,data,logo}' IS NOT NULL)
    """)) is not False:
        raise RuntimeError("Business data contains company logos; refusing downgrade")
    resize_profile_constraint(connection, 12000)
    connection.execute(sa.text(SAVE_0038))
