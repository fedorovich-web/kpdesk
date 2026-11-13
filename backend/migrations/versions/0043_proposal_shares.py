"""Frozen approved PDF capabilities; operator approval required."""
from alembic import op
from sqlalchemy import text

from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone
from kpdesk.db.proposal_share_schema import create_proposal_shares

revision = '0043_proposal_shares'
down_revision = '0042_openrouter_budget'
branch_labels = None
depends_on = None


def upgrade() -> None:
    config = op.get_context().config
    if config is None or config.attributes.get('kpdesk_proposal_shares_0043_approved') is not True:
        raise RuntimeError('Proposal share migration requires explicit approval')
    connection = op.get_bind()
    database, role, previous = connection.execute(text(
        'SELECT current_database(),current_user,(SELECT version_num FROM public.alembic_version)'
    )).one()
    if (not connection.in_transaction() or role != 'kpdesk' or previous != down_revision
            or (database != 'kpdesk' and not is_live_accounts_http_clone(connection))):
        raise RuntimeError('Unsafe proposal share migration target')
    connection.execute(text("SET LOCAL lock_timeout='5s'"))
    create_proposal_shares(connection)


def downgrade() -> None:
    raise RuntimeError('Issued document retention requires separately reviewed migration')
