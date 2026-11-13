"""Least-privilege schema readiness metadata; no runtime table grants."""
from alembic import op
from sqlalchemy import Connection, text

from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone
from kpdesk.db.runtime_readiness_schema import SIGNATURE, create_runtime_readiness

revision = '0041_runtime_readiness'
down_revision = '0040_training_feedback'
branch_labels = None
depends_on = None


def _preflight(expected: str) -> Connection:
    config = op.get_context().config
    if config is None or config.attributes.get('kpdesk_runtime_readiness_0041_approved') is not True:
        raise RuntimeError('Runtime readiness migration requires explicit approval')
    connection = op.get_bind()
    database,role,previous = connection.execute(text('''
        SELECT current_database(),current_user,(SELECT version_num FROM public.alembic_version)
    ''')).one()
    if (not connection.in_transaction() or role != 'kpdesk' or previous != expected
            or (database != 'kpdesk' and not is_live_accounts_http_clone(connection))):
        raise RuntimeError('Unsafe runtime readiness migration target')
    connection.execute(text("SET LOCAL lock_timeout='5s'"))
    return connection


def upgrade() -> None:
    create_runtime_readiness(_preflight(down_revision))


def downgrade() -> None:
    _preflight(revision).execute(text(f'DROP FUNCTION {SIGNATURE} RESTRICT'))
