"""Add auditable service owner user actions under explicit operator approval."""
from alembic import op
from sqlalchemy import text

from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone
from kpdesk.db.service_admin_schema import ADMIN_REVISION, service_admin_available
from kpdesk.db.service_admin_users import MODERATION_REVISION, create_service_admin_user_schema

revision = MODERATION_REVISION
down_revision = ADMIN_REVISION
branch_labels = None
depends_on = None


def upgrade() -> None:
    config = op.get_context().config
    if config is None or config.attributes.get('kpdesk_service_admin_users_0052_approved') is not True:
        raise RuntimeError('Service owner user migration requires explicit operator approval')
    connection = op.get_bind()
    if not connection.in_transaction():
        raise RuntimeError('Unsafe service owner user migration target')
    database, role, previous = connection.execute(text(
        'SELECT current_database(),current_user,(SELECT version_num FROM public.alembic_version)'
    )).one()
    working = config.attributes.get('kpdesk_service_admin_users_0052_working_database_approved') is True
    if (role != 'kpdesk' or previous != down_revision
            or not ((database == 'kpdesk' and working)
                    or (database != 'kpdesk' and is_live_accounts_http_clone(connection)))
            or not service_admin_available(connection)):
        raise RuntimeError('Unsafe service owner user migration target')
    connection.execute(text("SET LOCAL lock_timeout='5s'"))
    connection.execute(text("SET LOCAL statement_timeout='30s'"))
    create_service_admin_user_schema(connection)
    connection.execute(text(f"""CREATE OR REPLACE FUNCTION public.kpdesk_runtime_schema_revision()
        RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp
        AS $$ SELECT '{MODERATION_REVISION}'::text $$"""))


def downgrade() -> None:
    raise RuntimeError('Service owner audit retention requires a separately reviewed migration')
