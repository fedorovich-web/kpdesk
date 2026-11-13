"""Durable manager feedback; rollout requires separate explicit approval."""
from alembic import op
from sqlalchemy import Connection, text

from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone
from kpdesk.db.training_feedback_schema import (
    create_training_feedback_objects,
    drop_empty_training_feedback_objects,
)

revision = '0040_training_feedback'
down_revision = '0039_company_logo'
branch_labels = None
depends_on = None


def _preflight(expected: str) -> Connection:
    config = op.get_context().config
    if config is None or config.attributes.get('kpdesk_training_feedback_0040_approved') is not True:
        raise RuntimeError('Feedback migration requires explicit approval')
    connection = op.get_bind()
    database, role, previous = connection.execute(text('''
        SELECT current_database(),current_user,(SELECT version_num FROM public.alembic_version)
    ''')).one()
    if (not connection.in_transaction() or role != 'kpdesk' or previous != expected
            or (database != 'kpdesk' and not is_live_accounts_http_clone(connection))):
        raise RuntimeError('Unsafe feedback migration target')
    connection.execute(text("SET LOCAL lock_timeout='5s'"))
    return connection


def upgrade() -> None:
    create_training_feedback_objects(_preflight(down_revision))


def downgrade() -> None:
    drop_empty_training_feedback_objects(_preflight(revision))
