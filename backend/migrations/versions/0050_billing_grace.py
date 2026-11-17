"""Reviewed time-limited grace migration; never creates payment events."""
from alembic import op
from sqlalchemy import text

from kpdesk.billing.grace import create_grace_schema
from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone

revision = '0050_billing_grace'
down_revision = '0049_yandex_id'
branch_labels = None
depends_on = None


def upgrade() -> None:
    config = op.get_context().config
    if config is None or config.attributes.get('kpdesk_billing_grace_0050_approved') is not True:
        raise RuntimeError('Billing grace migration requires explicit operator approval')
    connection = op.get_bind()
    if not connection.in_transaction():
        raise RuntimeError('Unsafe billing grace migration target')
    database, role, previous = connection.execute(text(
        'SELECT current_database(),current_user,(SELECT version_num FROM public.alembic_version)'
    )).one()
    working = config.attributes.get('kpdesk_billing_grace_0050_working_database_approved') is True
    if (role != 'kpdesk' or previous != down_revision
            or not ((database == 'kpdesk' and working)
                    or (database != 'kpdesk' and is_live_accounts_http_clone(connection)))):
        raise RuntimeError('Unsafe billing grace migration target')
    connection.execute(text("SET LOCAL lock_timeout='5s'"))
    connection.execute(text("SET LOCAL statement_timeout='30s'"))
    create_grace_schema(connection)
    connection.execute(text("""CREATE OR REPLACE FUNCTION public.kpdesk_runtime_schema_revision()
        RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp
        AS $$ SELECT '0050_billing_grace'::text $$"""))


def downgrade() -> None:
    raise RuntimeError('Billing grace retention requires a separately reviewed migration')
