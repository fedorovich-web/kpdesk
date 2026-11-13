"""Recheck provider-operation authority after the global budget lock."""
from alembic import op
from sqlalchemy import text

from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone
from kpdesk.db.provider_budget_schema import _CLOSE, _FINISH, _RESERVE, SIGNATURES

revision = '0046_provider_authority'
down_revision = '0045_registry_retirement'
branch_labels = None
depends_on = None


def upgrade() -> None:
    config = op.get_context().config
    if config is None or config.attributes.get('kpdesk_provider_authority_0046_approved') is not True:
        raise RuntimeError('Provider authority migration requires explicit approval')
    connection = op.get_bind()
    database, role, previous = connection.execute(text(
        'SELECT current_database(),current_user,(SELECT version_num FROM public.alembic_version)',
    )).one()
    if (not connection.in_transaction() or role != 'kpdesk' or previous != down_revision
            or (database != 'kpdesk' and not is_live_accounts_http_clone(connection))):
        raise RuntimeError('Unsafe provider authority migration target')
    connection.execute(text("SET LOCAL lock_timeout='5s'"))
    for signature, definition in zip(SIGNATURES, (_RESERVE, _FINISH, _CLOSE), strict=True):
        if connection.scalar(text('SELECT to_regprocedure(:signature)'), {'signature': 'public.' + signature}) is None:
            raise RuntimeError('Provider authority migration cannot find the existing gateway')
        # Replacing the existing, fixed signatures preserves their ACLs and data.
        connection.execute(text(definition.replace('CREATE FUNCTION public.', 'CREATE OR REPLACE FUNCTION public.', 1)))
    connection.execute(text("""CREATE OR REPLACE FUNCTION public.kpdesk_runtime_schema_revision()
        RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp
        AS $$ SELECT '0046_provider_authority'::text $$"""))


def downgrade() -> None:
    raise RuntimeError('Removing the provider authority guard requires a separately reviewed migration')
