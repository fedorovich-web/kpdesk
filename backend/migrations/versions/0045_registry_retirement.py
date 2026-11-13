"""Revoke retired supplier-registry gateways without deleting historical rows."""
from alembic import op
from sqlalchemy import text

from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone
from kpdesk.db.supplier_registry_schema import REGISTRY_SIGNATURES

revision = '0045_registry_retirement'
down_revision = '0044_universal_products'
branch_labels = None
depends_on = None


def upgrade() -> None:
    config = op.get_context().config
    if config is None or config.attributes.get('kpdesk_registry_retirement_0045_approved') is not True:
        raise RuntimeError('Registry retirement requires explicit approval')
    connection = op.get_bind()
    database, role, previous = connection.execute(text(
        'SELECT current_database(),current_user,(SELECT version_num FROM public.alembic_version)',
    )).one()
    if (not connection.in_transaction() or role != 'kpdesk' or previous != down_revision
            or (database != 'kpdesk' and not is_live_accounts_http_clone(connection))):
        raise RuntimeError('Unsafe registry retirement target')
    connection.execute(text("SET LOCAL lock_timeout='5s'"))
    for signature in REGISTRY_SIGNATURES:
        if connection.scalar(text('SELECT to_regprocedure(:signature)'), {'signature': signature}) is None:
            raise RuntimeError('Registry retirement cannot find the existing gateway')
        # Identifiers are the fixed schema signatures shared with 0036, never caller values.
        connection.execute(text(f'REVOKE ALL ON FUNCTION {signature} '
            'FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime'))
    connection.execute(text("""REVOKE ALL ON public.tenant_supplier_registry
        FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime"""))
    connection.execute(text("""CREATE OR REPLACE FUNCTION public.kpdesk_runtime_schema_revision()
        RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp
        AS $$ SELECT '0045_registry_retirement'::text $$"""))


def downgrade() -> None:
    raise RuntimeError('Restoring retired SQL authority requires a separately reviewed migration')
