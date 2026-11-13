"""Universal catalogs and draft snapshots require explicit operator approval."""
from alembic import op
from sqlalchemy import text

from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone
from kpdesk.db.universal_schema import create_universal_schema

revision='0044_universal_products'
down_revision='0043_proposal_shares'
branch_labels=None
depends_on=None


def upgrade() -> None:
    config=op.get_context().config
    if config is None or config.attributes.get('kpdesk_universal_0044_approved') is not True:
        raise RuntimeError('Universal product migration requires explicit approval')
    connection=op.get_bind()
    database,role,previous=connection.execute(text(
        'SELECT current_database(),current_user,(SELECT version_num FROM public.alembic_version)'
    )).one()
    if (not connection.in_transaction() or role!='kpdesk' or previous!=down_revision
            or (database!='kpdesk' and not is_live_accounts_http_clone(connection))):
        raise RuntimeError('Unsafe universal product migration target')
    connection.execute(text("SET LOCAL lock_timeout='5s'"))
    create_universal_schema(connection)


def downgrade() -> None:
    raise RuntimeError('Universal history retention requires a separately reviewed migration')
