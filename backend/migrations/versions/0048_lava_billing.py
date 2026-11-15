"""Opt-in Lava billing ledger and transactional quotas. Never installed at startup."""
from alembic import op
from sqlalchemy import text

from kpdesk.billing.schema import create_billing_schema
from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone

revision = '0048_lava_billing'
down_revision = '0047_auth_reset_serialization'
branch_labels = None
depends_on = None


def upgrade() -> None:
    config = op.get_context().config
    if config is None or config.attributes.get('kpdesk_lava_billing_0048_approved') is not True:
        raise RuntimeError('Billing migration requires explicit approval')
    connection = op.get_bind()
    if not connection.in_transaction():
        raise RuntimeError('Unsafe billing migration target')
    database, role, previous = connection.execute(text(
        'SELECT current_database(),current_user,(SELECT version_num FROM public.alembic_version)'
    )).one()
    # Working database permission is distinct from approval to rehearse a clone.
    working_approved = config.attributes.get('kpdesk_lava_billing_0048_working_database_approved') is True
    if (role != 'kpdesk' or previous != down_revision
            or not ((database == 'kpdesk' and working_approved)
                    or is_live_accounts_http_clone(connection))):
        raise RuntimeError('Unsafe billing migration target')
    connection.execute(text("SET LOCAL lock_timeout='5s'"))
    connection.execute(text("SET LOCAL statement_timeout='30s'"))
    create_billing_schema(connection)
    connection.execute(text("""CREATE OR REPLACE FUNCTION public.kpdesk_runtime_schema_revision()
        RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp
        AS $$ SELECT '0048_lava_billing'::text $$"""))


def downgrade() -> None:
    raise RuntimeError('Payment ledger retention requires a separately reviewed migration')
