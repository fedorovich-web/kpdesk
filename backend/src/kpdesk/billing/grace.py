"""Owner-managed, time-limited legacy access. Never fabricates a payment or grant."""
from __future__ import annotations

from sqlalchemy import Connection, text

from kpdesk.billing.schema import _QUOTA, _SUMMARY

_GRACE_DDL = """
CREATE TABLE public.billing_grace (
  organization_id uuid PRIMARY KEY REFERENCES public.organizations(id) ON DELETE RESTRICT,
  starts_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  ends_at timestamptz NOT NULL,
  reason text NOT NULL CHECK (length(reason) BETWEEN 8 AND 160),
  issued_by text NOT NULL CHECK (length(issued_by) BETWEEN 3 AND 80),
  revoked_at timestamptz,
  CHECK (ends_at > starts_at AND ends_at <= starts_at + interval '30 days'),
  CHECK (revoked_at IS NULL OR revoked_at >= starts_at)
)"""

_GRACE_ACTIVE = """CREATE FUNCTION public.kpdesk_billing_grace_active(p_org uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
  SELECT EXISTS (
    SELECT 1 FROM public.billing_grace g
    WHERE g.organization_id=p_org
      AND g.revoked_at IS NULL AND g.starts_at<=clock_timestamp() AND g.ends_at>clock_timestamp()
      AND NOT EXISTS (
        SELECT 1 FROM public.billing_grants paid
        WHERE paid.organization_id=p_org AND paid.revoked_at IS NULL
          AND paid.starts_at<=clock_timestamp() AND paid.ends_at>clock_timestamp()
      )
  )
$$"""

# Rev 0049 quota checks remain untouched except for one explicitly scoped exemption.
# Purchased subscriptions always supersede a temporary legacy grace.
_GRACE_QUOTA = _QUOTA.replace(
    " grant_row:=public.kpdesk_billing_entitlement(NEW.organization_id);",
    " IF public.kpdesk_billing_grace_active(NEW.organization_id) THEN RETURN NEW; END IF;\n"
    " grant_row:=public.kpdesk_billing_entitlement(NEW.organization_id);",
).replace("CREATE FUNCTION public.kpdesk_billing_quota_guard()", "CREATE OR REPLACE FUNCTION public.kpdesk_billing_quota_guard()", 1)

# The payment history remains unchanged and clearly distinguishes nonpayment access.
_GRACE_SUMMARY = _SUMMARY.replace(
    "   'subscription',CASE WHEN active.order_id IS NULL",
    "   'grace_ends_at',(SELECT ends_at FROM public.billing_grace g"
    " WHERE g.organization_id=p_org AND public.kpdesk_billing_grace_active(p_org)),"
    "   'subscription',CASE WHEN active.order_id IS NULL",
).replace("CREATE FUNCTION public.kpdesk_billing_summary(", "CREATE OR REPLACE FUNCTION public.kpdesk_billing_summary(", 1)

if (_GRACE_QUOTA == _QUOTA or _GRACE_SUMMARY == _SUMMARY
        or _GRACE_QUOTA.count('kpdesk_billing_grace_active') != 1):
    raise RuntimeError("billing_grace_definition_anchor_missing")


def create_grace_schema(connection: Connection) -> None:
    """Only the reviewed 0050 migration may call this DDL."""
    connection.execute(text(_GRACE_DDL))
    for statement in (
        "REVOKE ALL ON TABLE public.billing_grace FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime",
        "ALTER TABLE public.billing_grace ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE public.billing_grace FORCE ROW LEVEL SECURITY",
        "CREATE POLICY billing_grace_owner ON public.billing_grace TO kpdesk USING(true) WITH CHECK(true)",
        _GRACE_ACTIVE,
        "REVOKE ALL ON FUNCTION public.kpdesk_billing_grace_active(uuid)"
        " FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime",
        _GRACE_QUOTA,
        _GRACE_SUMMARY,
    ):
        connection.execute(text(statement))


def grace_available(connection: Connection) -> bool:
    """Read-only verification of isolation and revised public gateways."""
    return connection.scalar(text("""
      SELECT
        (SELECT c.relrowsecurity AND c.relforcerowsecurity
          AND pg_get_userbyid(c.relowner)='kpdesk'
          AND NOT has_table_privilege('kpdesk_tenant_runtime',c.oid,'SELECT,INSERT,UPDATE,DELETE')
          AND NOT has_table_privilege('kpdesk_auth_runtime',c.oid,'SELECT,INSERT,UPDATE,DELETE')
          FROM pg_catalog.pg_class c WHERE c.oid=to_regclass('public.billing_grace'))
        AND (SELECT p.prosecdef
          AND p.proconfig=ARRAY['search_path=pg_catalog, pg_temp']::text[]
          AND NOT has_function_privilege('kpdesk_tenant_runtime',p.oid,'EXECUTE')
          AND NOT has_function_privilege('kpdesk_auth_runtime',p.oid,'EXECUTE')
          AND NOT EXISTS (
            SELECT 1 FROM aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
            WHERE a.grantee=0 AND a.privilege_type='EXECUTE')
          FROM pg_catalog.pg_proc p
          WHERE p.oid=to_regprocedure('public.kpdesk_billing_grace_active(uuid)'))
        AND (SELECT position('kpdesk_billing_grace_active' in pg_get_functiondef(
          'public.kpdesk_billing_quota_guard()'::regprocedure)) > 0)
        AND (SELECT position('grace_ends_at' in pg_get_functiondef(
          'public.kpdesk_billing_summary(uuid)'::regprocedure)) > 0)
    """)) is True
