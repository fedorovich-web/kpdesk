"""Exact PostgreSQL supplier-directory DDL shared by Alembic and clone E2E.

Callers own a transaction and must separately prove schema-owner authorization.
The clone helper never changes the working database or Alembic revision.
"""
from __future__ import annotations

from sqlalchemy import Connection, text

from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone
from kpdesk.db.supplier_registry_sql import (
    FUNCTION_LIST,
    FUNCTION_STATUS,
    FUNCTION_UPSERT,
)

_LIST = "public.kpdesk_supplier_registry_list(uuid,integer,integer)"
_UPSERT = "public.kpdesk_supplier_registry_upsert(uuid,text,text,text,text,integer)"
_STATUS = "public.kpdesk_supplier_registry_status(uuid,text)"

_TABLE_STEPS = (
    """
    CREATE TABLE public.tenant_supplier_registry (
      organization_id uuid NOT NULL
        REFERENCES public.organizations(id) ON DELETE RESTRICT,
      supplier_id varchar(120) NOT NULL,
      display_name varchar(200) NOT NULL,
      notes varchar(1000) NOT NULL DEFAULT '',
      status text NOT NULL DEFAULT 'active',
      revision integer NOT NULL DEFAULT 1,
      created_by uuid NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
      updated_by uuid NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      CONSTRAINT pk_tenant_supplier_registry PRIMARY KEY (
        organization_id,supplier_id
      ),
      CONSTRAINT ck_supplier_registry_id
        CHECK (supplier_id ~ '^[A-Za-z0-9_.-]{1,120}$'),
      CONSTRAINT ck_supplier_registry_name
        CHECK (length(btrim(display_name)) BETWEEN 1 AND 200),
      CONSTRAINT ck_supplier_registry_notes CHECK (length(notes) <= 1000),
      CONSTRAINT ck_supplier_registry_status
        CHECK (status IN ('active','archived')),
      CONSTRAINT ck_supplier_registry_revision CHECK (revision > 0)
    )
    """,
    """
    CREATE INDEX ix_supplier_registry_name
    ON public.tenant_supplier_registry (
      organization_id,lower(display_name),supplier_id
    )
    """,
    """
    REVOKE ALL ON TABLE public.tenant_supplier_registry
    FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime
    """,
    "ALTER TABLE public.tenant_supplier_registry ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE public.tenant_supplier_registry FORCE ROW LEVEL SECURITY",
    """
    CREATE POLICY supplier_registry_owner_only ON public.tenant_supplier_registry
    TO kpdesk USING (true) WITH CHECK (true)
    """,
)
_FUNCTIONS = (
    (FUNCTION_LIST, _LIST),
    (FUNCTION_UPSERT, _UPSERT),
    (FUNCTION_STATUS, _STATUS),
)
REGISTRY_SIGNATURES = tuple(signature for _, signature in _FUNCTIONS)


def registry_retired(connection: Connection) -> bool:
    """Read-only check that the active role cannot use the retired directory."""
    return connection.scalar(text("""SELECT count(*)=3 AND bool_and(
        to_regprocedure(signature) IS NOT NULL
        AND NOT has_function_privilege(current_user,to_regprocedure(signature),'EXECUTE'))
        AND to_regclass('public.tenant_supplier_registry') IS NOT NULL
        AND NOT has_any_column_privilege(current_user,to_regclass('public.tenant_supplier_registry'),'SELECT')
        AND NOT has_any_column_privilege(current_user,to_regclass('public.tenant_supplier_registry'),'INSERT')
        AND NOT has_any_column_privilege(current_user,to_regclass('public.tenant_supplier_registry'),'UPDATE')
        AND NOT has_table_privilege(current_user,to_regclass('public.tenant_supplier_registry'),'DELETE')
        FROM unnest(CAST(:signatures AS text[])) AS retired(signature)"""),
        {'signatures': list(REGISTRY_SIGNATURES)}) is True


def create_supplier_directory_objects(connection: Connection) -> None:
    """Requires a caller-owned transaction and separately authorized schema owner."""
    if connection.scalar(text(
        "SELECT to_regclass('public.tenant_supplier_registry')"
    )) is not None:
        raise RuntimeError("Supplier registry already installed.")
    for statement in _TABLE_STEPS:
        connection.execute(text(statement))
    for statement, signature in _FUNCTIONS:
        if connection.scalar(text("SELECT to_regprocedure(:signature)"), {
            "signature": signature,
        }) is not None:
            raise RuntimeError("Supplier registry function already exists.")
        connection.execute(text(statement))
        connection.execute(text(f"""
            REVOKE ALL ON FUNCTION {signature}
            FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime
        """))
        connection.execute(text(f"""
            GRANT EXECUTE ON FUNCTION {signature} TO kpdesk_tenant_runtime
        """))


def install_supplier_registry_on_clone(connection: Connection) -> None:
    """A45 positive E2E: same DDL but without altering its 0032 Alembic stamp."""
    if (
        not connection.in_transaction()
        or not is_live_accounts_http_clone(connection)
        or connection.scalar(text("SELECT current_user")) != "kpdesk"
        or connection.scalar(text(
            "SELECT version_num FROM public.alembic_version"
        )) != "0032_supplier_quote_audit"
    ):
        raise RuntimeError("Supplier registry rehearsal requires a tagged 0032 clone.")
    with connection.begin_nested():
        create_supplier_directory_objects(connection)
