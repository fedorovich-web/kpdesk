"""Install the service owner read gateway with an empty owner allowlist."""
from alembic import op
from sqlalchemy import text

from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone
from kpdesk.db.service_admin_schema import create_service_admin_schema

revision = '0051_service_admin'
down_revision = '0050_billing_grace'
branch_labels = None
depends_on = None


def upgrade() -> None:
    config = op.get_context().config
    if config is None or config.attributes.get('kpdesk_service_admin_0051_approved') is not True:
        raise RuntimeError('Service owner migration requires explicit operator approval')
    connection = op.get_bind()
    if not connection.in_transaction():
        raise RuntimeError('Unsafe service owner migration target')
    database, role, previous = connection.execute(text(
        'SELECT current_database(),current_user,(SELECT version_num FROM public.alembic_version)'
    )).one()
    working = config.attributes.get('kpdesk_service_admin_0051_working_database_approved') is True
    if (role != 'kpdesk' or previous != down_revision
            or not ((database == 'kpdesk' and working)
                    or (database != 'kpdesk' and is_live_accounts_http_clone(connection)))):
        raise RuntimeError('Unsafe service owner migration target')
    connection.execute(text("SET LOCAL lock_timeout='5s'"))
    connection.execute(text("SET LOCAL statement_timeout='30s'"))
    create_service_admin_schema(connection)
    connection.execute(text("""CREATE OR REPLACE FUNCTION public.kpdesk_runtime_schema_revision()
        RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp
        AS $$ SELECT '0051_service_admin'::text $$"""))


def downgrade() -> None:
    raise RuntimeError('Service owner retention requires a separately reviewed migration')
