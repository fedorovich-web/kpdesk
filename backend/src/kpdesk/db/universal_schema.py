"""Opt-in 0044 universal records. Startup never runs this installer."""
from sqlalchemy import Connection, text

TABLES=('universal_catalogs','universal_quote_revisions','universal_feedback')


def universal_available(connection: Connection) -> bool:
    return connection.scalar(text("""SELECT count(*)=3 FROM pg_catalog.pg_class
        WHERE relrowsecurity AND relforcerowsecurity AND relnamespace='public'::regnamespace
        AND relname IN ('universal_catalogs','universal_quote_revisions','universal_feedback')""")) is True


def create_universal_schema(connection: Connection) -> None:
    statements=[
        """CREATE TABLE public.universal_catalogs (
          organization_id uuid NOT NULL REFERENCES public.organizations(id) ON DELETE RESTRICT,
          id uuid NOT NULL, source_sha256 text NOT NULL CHECK(source_sha256 ~ '^[0-9a-f]{64}$'),
          payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND octet_length(payload::text)<=26214400),
          idempotency_key uuid NOT NULL, request_sha256 text NOT NULL CHECK(request_sha256 ~ '^[0-9a-f]{64}$'),
          created_by uuid NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
          created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          PRIMARY KEY(organization_id,id), UNIQUE(organization_id,idempotency_key))""",
        """CREATE TABLE public.universal_quote_revisions (
          organization_id uuid NOT NULL REFERENCES public.organizations(id) ON DELETE RESTRICT,
          quote_id uuid NOT NULL, revision integer NOT NULL CHECK(revision BETWEEN 1 AND 2147483647),
          catalog_version_id uuid NOT NULL,
          payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND octet_length(payload::text)<=1048576),
          snapshot_sha256 text NOT NULL CHECK(snapshot_sha256 ~ '^[0-9a-f]{64}$'),
          idempotency_key uuid NOT NULL, request_sha256 text NOT NULL CHECK(request_sha256 ~ '^[0-9a-f]{64}$'),
          created_by uuid NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
          created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          PRIMARY KEY(organization_id,quote_id,revision), UNIQUE(organization_id,idempotency_key),
          FOREIGN KEY(organization_id,catalog_version_id) REFERENCES public.universal_catalogs(organization_id,id) ON DELETE RESTRICT)""",
        """CREATE INDEX universal_quote_heads ON public.universal_quote_revisions(organization_id,quote_id,revision DESC)""",
        """CREATE TABLE public.universal_feedback (
          organization_id uuid NOT NULL, id uuid NOT NULL, quote_id uuid NOT NULL, revision integer NOT NULL,
          payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND octet_length(payload::text)<=1048576),
          idempotency_key uuid NOT NULL, request_sha256 text NOT NULL CHECK(request_sha256 ~ '^[0-9a-f]{64}$'),
          created_by uuid NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
          created_at timestamptz NOT NULL DEFAULT clock_timestamp(), withdrawn_at timestamptz,
          PRIMARY KEY(organization_id,id), UNIQUE(organization_id,idempotency_key),
          FOREIGN KEY(organization_id,quote_id,revision) REFERENCES public.universal_quote_revisions(organization_id,quote_id,revision) ON DELETE RESTRICT)""",
    ]
    for statement in statements:connection.execute(text(statement))
    for table in TABLES:
        for statement in (
            f'REVOKE ALL ON public.{table} FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime',
            f'ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY',
            f'ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY',
            f'CREATE POLICY {table}_owner ON public.{table} TO kpdesk USING(true) WITH CHECK(true)',
            f"CREATE POLICY {table}_read ON public.{table} FOR SELECT TO kpdesk_tenant_runtime USING(public.kpdesk_session_tenant_allowed(organization_id,'read'))",
            f"CREATE POLICY {table}_insert ON public.{table} FOR INSERT TO kpdesk_tenant_runtime WITH CHECK(public.kpdesk_session_tenant_allowed(organization_id,'write'))",
            f'GRANT SELECT,INSERT ON public.{table} TO kpdesk_tenant_runtime',
        ):connection.execute(text(statement))
    connection.execute(text("""CREATE POLICY universal_feedback_withdraw ON public.universal_feedback
        FOR UPDATE TO kpdesk_tenant_runtime
        USING(withdrawn_at IS NULL AND public.kpdesk_session_tenant_allowed(organization_id,'write'))
        WITH CHECK(withdrawn_at IS NOT NULL AND public.kpdesk_session_tenant_allowed(organization_id,'write'))"""))
    connection.execute(text('GRANT UPDATE(withdrawn_at) ON public.universal_feedback TO kpdesk_tenant_runtime'))
    connection.execute(text("""CREATE OR REPLACE FUNCTION public.kpdesk_runtime_schema_revision()
        RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp
        AS $$ SELECT '0044_universal_products'::text $$"""))
