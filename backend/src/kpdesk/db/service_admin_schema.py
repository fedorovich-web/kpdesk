"""Read gateway for service owners. Only the explicit migration installs it."""
from sqlalchemy import Connection, text

ADMIN_REVISION = '0051_service_admin'
SIGNATURE = 'public.kpdesk_service_admin_read(text,text,integer,text,integer)'

_TABLE = """
CREATE TABLE public.service_admin_grants (
    user_id uuid PRIMARY KEY REFERENCES public.users(id) ON DELETE RESTRICT,
    granted_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    revoked_at timestamptz,
    CHECK (revoked_at IS NULL OR revoked_at >= granted_at)
)
"""

_GATEWAY = f"""
CREATE FUNCTION {SIGNATURE.split('(')[0]}(
    p_hash text, p_section text, p_days integer, p_query text, p_page integer
) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE
    actor uuid;
    captured_at timestamptz := statement_timestamp();
    since_at timestamptz;
    result jsonb;
BEGIN
    SELECT s.user_id INTO actor
    FROM public.auth_sessions s
    JOIN public.users u ON u.id = s.user_id
    WHERE s.token_sha256 = p_hash AND s.revoked_at IS NULL
      AND s.expires_at > captured_at AND u.status = 'active'
      AND u.email_verified_at IS NOT NULL;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('access', 'invalid_session');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.service_admin_grants g
                   WHERE g.user_id = actor AND g.revoked_at IS NULL) THEN
        RETURN jsonb_build_object('access', 'forbidden');
    END IF;
    IF p_days IS NULL OR p_days NOT IN (7,30,90)
        OR p_page IS NULL OR p_page NOT BETWEEN 1 AND 1000
        OR p_query IS NULL OR length(p_query) > 100
        OR p_section IS NULL OR p_section NOT IN ('capability','overview','users','organizations','payments','reversals')
    THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid service admin request';
    END IF;
    since_at := captured_at - make_interval(hours => p_days * 24);
    IF p_section = 'capability' THEN
        result := jsonb_build_object('allowed', true);
    ELSIF p_section = 'overview' THEN
        WITH active_paid AS (
            SELECT DISTINCT ON (g.organization_id) g.organization_id, g.plan_id, g.ends_at
            FROM public.billing_grants g JOIN public.organizations o ON o.id = g.organization_id
            WHERE o.status = 'active' AND g.revoked_at IS NULL
              AND g.starts_at <= captured_at AND g.ends_at > captured_at
            ORDER BY g.organization_id, g.ends_at DESC, g.order_id
        ), confirmed_receipts AS (
            SELECT e.order_id, min(e.observed_at) AS confirmed_at
            FROM public.billing_events e WHERE e.status = 'paid' GROUP BY e.order_id
        ), receipts AS (
            SELECT o.currency, count(*) AS count, sum(o.amount_minor)::text AS amount_minor
            FROM confirmed_receipts r JOIN public.billing_orders o ON o.order_id = r.order_id
            WHERE r.confirmed_at >= since_at AND r.confirmed_at <= captured_at GROUP BY o.currency
        ), reversals AS (
            SELECT r.currency, count(*) AS count, sum(r.amount_minor)::text AS amount_minor
            FROM public.billing_reversals r
            WHERE r.event_type = 'refund.success' AND r.resolved_at IS NOT NULL
              AND r.received_at >= since_at AND r.received_at <= captured_at GROUP BY r.currency
        )
        SELECT jsonb_build_object(
            'as_of', captured_at, 'period_days', p_days,
            'users_total', (SELECT count(*) FROM public.users),
            'users_new', (SELECT count(*) FROM public.users WHERE created_at BETWEEN since_at AND captured_at),
            'users_verified', (SELECT count(*) FROM public.users WHERE status='active' AND email_verified_at IS NOT NULL),
            'organizations_total', (SELECT count(*) FROM public.organizations),
            'organizations_active', (SELECT count(*) FROM public.organizations WHERE status='active'),
            'paid_organizations', (SELECT count(*) FROM active_paid),
            'expiring_7', (SELECT count(*) FROM active_paid a
                WHERE a.ends_at <= captured_at + interval '168 hours'
                  AND NOT EXISTS (SELECT 1 FROM public.billing_grants g
                      WHERE g.organization_id=a.organization_id AND g.revoked_at IS NULL
                        AND g.starts_at <= a.ends_at AND g.ends_at > a.ends_at)),
            'plans', (SELECT coalesce(jsonb_agg(to_jsonb(p) ORDER BY p.plan_id), '[]'::jsonb)
                FROM (SELECT plan_id, count(*) AS count FROM active_paid GROUP BY plan_id) p),
            'receipts', (SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY r.currency), '[]'::jsonb) FROM receipts r),
            'refunds', (SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY r.currency), '[]'::jsonb) FROM reversals r),
            'unresolved_reversals', (SELECT count(*) FROM public.billing_reversals WHERE resolved_at IS NULL),
            'ambiguous_orders', (SELECT count(*) FROM public.billing_orders WHERE status='ambiguous'),
            'quotes_new', (SELECT count(*) FROM public.universal_quote_revisions
                WHERE revision=1 AND created_at BETWEEN since_at AND captured_at),
            'organizations_with_catalog', (SELECT count(DISTINCT organization_id) FROM public.universal_catalogs),
            'ai_attempts', (SELECT count(*) FROM public.provider_budget_attempts
                WHERE status NOT IN ('reserved','cancelled') AND created_at BETWEEN since_at AND captured_at),
            'ai_success', (SELECT count(*) FROM public.provider_budget_attempts
                WHERE status='success' AND created_at BETWEEN since_at AND captured_at)
        ) INTO result;
    ELSE
        WITH matched AS (
            SELECT u.id::text AS id, u.created_at AS stamp
            FROM public.users u WHERE p_section='users'
                AND strpos(lower(u.email), p_query)>0
            UNION ALL
            SELECT o.id::text, o.created_at FROM public.organizations o
            WHERE p_section='organizations' AND strpos(lower(o.name),p_query)>0
            UNION ALL
            SELECT b.order_id::text, b.created_at
            FROM public.billing_orders b JOIN public.organizations o ON o.id=b.organization_id
            WHERE p_section='payments' AND (strpos(lower(o.name),p_query)>0 OR strpos(b.order_id::text,p_query)>0)
            UNION ALL
            SELECT r.event_id::text, r.received_at FROM public.billing_reversals r
            WHERE p_section='reversals' AND (strpos(r.event_id::text,p_query)>0 OR strpos(r.event_type,p_query)>0)
        ), page AS (
            SELECT id, stamp FROM matched ORDER BY stamp DESC, id DESC LIMIT 25 OFFSET (p_page-1)*25
        ), projected AS (
            SELECT p.id, p.stamp, CASE p_section
                WHEN 'users' THEN (SELECT jsonb_build_object(
                    'id', u.id, 'email', u.email, 'status', u.status, 'created_at', u.created_at,
                    'email_verified_at', u.email_verified_at, 'organizations_count',
                    (SELECT count(*) FROM public.organization_memberships m WHERE m.user_id=u.id AND m.status='active'))
                    FROM public.users u WHERE u.id=p.id::uuid)
                WHEN 'organizations' THEN (SELECT jsonb_build_object(
                    'id', o.id, 'name', o.name, 'status', o.status, 'created_at', o.created_at,
                    'members', (SELECT count(*) FROM public.organization_memberships m
                        WHERE m.organization_id=o.id AND m.status='active'),
                    'quotes', (SELECT count(*) FROM public.universal_quote_revisions q
                        WHERE q.organization_id=o.id AND q.revision=1),
                    'plan_id', paid.plan_id, 'paid_through', paid.ends_at)
                    FROM public.organizations o LEFT JOIN LATERAL (
                        SELECT g.plan_id, g.ends_at FROM public.billing_grants g
                        WHERE o.status='active' AND g.organization_id=o.id AND g.revoked_at IS NULL
                          AND g.starts_at<=captured_at AND g.ends_at>captured_at
                        ORDER BY g.ends_at DESC, g.order_id LIMIT 1
                    ) paid ON true WHERE o.id=p.id::uuid)
                WHEN 'payments' THEN (SELECT jsonb_build_object(
                    'order_id', b.order_id, 'organization_name', o.name, 'plan_id', b.plan_id,
                    'amount_minor', b.amount_minor::text, 'currency', b.currency, 'status', b.status,
                    'created_at', b.created_at, 'confirmed_at',
                    (SELECT min(e.observed_at) FROM public.billing_events e WHERE e.order_id=b.order_id AND e.status='paid'))
                    FROM public.billing_orders b JOIN public.organizations o ON o.id=b.organization_id WHERE b.order_id=p.id::uuid)
                WHEN 'reversals' THEN (SELECT jsonb_build_object(
                    'event_id', r.event_id, 'event_type', r.event_type, 'amount_minor', r.amount_minor::text,
                    'currency', r.currency, 'received_at', r.received_at, 'resolved', r.resolved_at IS NOT NULL,
                    'order_id', r.resolved_order_id)
                    FROM public.billing_reversals r WHERE r.event_id=p.id::uuid)
            END AS item FROM page p
        )
        SELECT jsonb_build_object('total', (SELECT count(*) FROM matched),
            'items', coalesce(jsonb_agg(item ORDER BY stamp DESC,id DESC), '[]'::jsonb))
        INTO result FROM projected;
    END IF;
    RETURN jsonb_build_object('access', 'allowed', 'data', result);
END
$$
"""


def create_service_admin_schema(connection: Connection) -> None:
    for statement in (
        _TABLE,
        'ALTER TABLE public.service_admin_grants ENABLE ROW LEVEL SECURITY',
        'ALTER TABLE public.service_admin_grants FORCE ROW LEVEL SECURITY',
        'CREATE POLICY service_admin_owner ON public.service_admin_grants TO kpdesk USING(true) WITH CHECK(true)',
        'REVOKE ALL ON public.service_admin_grants FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime',
        _GATEWAY,
        f'REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime',
        f'GRANT EXECUTE ON FUNCTION {SIGNATURE} TO kpdesk_tenant_runtime',
    ):
        connection.execute(text(statement))


def service_admin_available(connection: Connection) -> bool:
    return connection.scalar(text(f"""
        SELECT
          (SELECT c.relrowsecurity AND c.relforcerowsecurity AND pg_get_userbyid(c.relowner)='kpdesk'
             AND NOT has_table_privilege('kpdesk_tenant_runtime',c.oid,'SELECT,INSERT,UPDATE,DELETE')
             AND NOT has_table_privilege('kpdesk_auth_runtime',c.oid,'SELECT,INSERT,UPDATE,DELETE')
           FROM pg_catalog.pg_class c WHERE c.oid=to_regclass('public.service_admin_grants'))
          AND
          (SELECT p.prosecdef AND pg_get_userbyid(p.proowner)='kpdesk'
             AND p.proconfig=ARRAY['search_path=pg_catalog, pg_temp']::text[]
             AND has_function_privilege('kpdesk_tenant_runtime',p.oid,'EXECUTE')
             AND NOT has_function_privilege('kpdesk_auth_runtime',p.oid,'EXECUTE')
             AND NOT EXISTS (SELECT 1 FROM aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
                             WHERE a.grantee=0 AND a.privilege_type='EXECUTE')
           FROM pg_catalog.pg_proc p WHERE p.oid=to_regprocedure('{SIGNATURE}'))
    """)) is True
