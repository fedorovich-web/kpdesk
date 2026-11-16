"""Opt-in Yandex ID persistence; never installed by application startup."""
from alembic import op
from sqlalchemy import text

from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone
from kpdesk.yandex.schema import create_yandex_schema

revision = '0049_yandex_id'
down_revision = '0048_lava_billing'
branch_labels = None
depends_on = None


def upgrade() -> None:
    config = op.get_context().config
    if config is None or config.attributes.get('kpdesk_yandex_id_0049_approved') is not True:
        raise RuntimeError('Yandex migration requires explicit approval')
    connection = op.get_bind()
    if not connection.in_transaction():
        raise RuntimeError('Unsafe Yandex migration target')
    database, role, previous = connection.execute(text(
        'SELECT current_database(),current_user,(SELECT version_num FROM public.alembic_version)'
    )).one()
    working_approved = config.attributes.get('kpdesk_yandex_id_0049_working_database_approved') is True
    if (role != 'kpdesk' or previous != down_revision
            or not ((database == 'kpdesk' and working_approved)
                    or (database != 'kpdesk' and is_live_accounts_http_clone(connection)))):
        raise RuntimeError('Unsafe Yandex migration target')
    connection.execute(text("SET LOCAL lock_timeout='5s'"))
    connection.execute(text("SET LOCAL statement_timeout='30s'"))
    create_yandex_schema(connection)
    connection.execute(text("""CREATE OR REPLACE FUNCTION public.kpdesk_runtime_schema_revision()
        RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp
        AS $$ SELECT '0049_yandex_id'::text $$"""))


def downgrade() -> None:
    raise RuntimeError('Yandex identity retention requires a separately reviewed migration')
