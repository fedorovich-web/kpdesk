"""Narrow schema metadata interface; runtime keeps no SELECT on Alembic tables."""
from sqlalchemy import Connection, text

from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone

SIGNATURE = 'public.kpdesk_runtime_schema_revision()'
CREATE = '''CREATE FUNCTION public.kpdesk_runtime_schema_revision() RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT CASE WHEN count(*)=1 THEN min(version_num)::text ELSE NULL END
 FROM public.alembic_version
$$'''


def create_runtime_readiness(connection: Connection) -> None:
    connection.execute(text(CREATE))
    connection.execute(text(f'REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime'))
    connection.execute(text(f'GRANT EXECUTE ON FUNCTION {SIGNATURE} TO kpdesk_tenant_runtime'))


def install_runtime_readiness_on_clone(connection: Connection) -> None:
    if (not connection.in_transaction() or not is_live_accounts_http_clone(connection)
            or connection.scalar(text('SELECT current_user')) != 'kpdesk'):
        raise RuntimeError('Runtime readiness installer requires tagged owner clone')
    create_runtime_readiness(connection)
