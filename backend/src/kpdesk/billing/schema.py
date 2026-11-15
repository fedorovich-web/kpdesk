"""Explicitly installed billing ledger and cross-path quota enforcement.

Runtime roles receive only narrow SECURITY DEFINER gateways. Activation and
provider offer IDs are persisted operator configuration, never request GUCs.
"""
from __future__ import annotations

import json

from sqlalchemy import Connection, text

BILLING_TABLES = ('billing_config', 'billing_plans', 'billing_offers', 'billing_orders',
                  'billing_events', 'billing_grants', 'billing_usage', 'billing_reversals')


def billing_available(connection: Connection) -> bool:
    """Read-only verification of the full restricted billing/guard installation."""
    functions = [{'signature': 'public.' + signature, 'roles': list(roles)}
                 for signature, _definition, roles in _FUNCTIONS]
    triggers = [{'table_name': table, 'trigger_name': 'billing_quota_guard',
                 'function_name': 'public.kpdesk_billing_quota_guard()', 'trigger_type': trigger_type,
                 'attributes': attributes}
                for table, _timing, _operation, trigger_type, attributes in _QUOTA_TRIGGERS]
    triggers.extend({'table_name': table, 'trigger_name': 'billing_immutable',
                     'function_name': 'public.kpdesk_billing_immutable()', 'trigger_type': 27,
                     'attributes': []} for table in _IMMUTABLE_TABLES)
    return connection.scalar(text("""SELECT
      (SELECT count(*)=:table_count FROM pg_catalog.pg_class c
       WHERE c.relnamespace='public'::regnamespace AND c.relname=ANY(CAST(:tables AS text[]))
       AND c.relrowsecurity AND c.relforcerowsecurity AND pg_get_userbyid(c.relowner)='kpdesk'
       AND NOT EXISTS(SELECT 1 FROM unnest(ARRAY['kpdesk_app','kpdesk_auth_runtime','kpdesk_tenant_runtime']) r(role)
           WHERE has_table_privilege(r.role,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE')))
      AND (SELECT count(*)=:function_count FROM jsonb_to_recordset(CAST(:functions AS jsonb))
          e(signature text,roles text[]) JOIN pg_catalog.pg_proc p ON p.oid=to_regprocedure(e.signature)
       WHERE p.prosecdef AND p.proconfig=ARRAY['search_path=pg_catalog, pg_temp']::text[]
       AND pg_get_userbyid(p.proowner)='kpdesk'
       AND NOT EXISTS(SELECT 1 FROM aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
                      WHERE a.grantee=0 AND a.privilege_type='EXECUTE')
       AND NOT EXISTS(SELECT 1 FROM unnest(ARRAY['kpdesk_app','kpdesk_auth_runtime','kpdesk_tenant_runtime']) r(role)
          WHERE has_function_privilege(r.role,p.oid,'EXECUTE') IS DISTINCT FROM (r.role=ANY(e.roles))))
      AND (SELECT count(*)=:trigger_count FROM jsonb_to_recordset(CAST(:triggers AS jsonb))
          e(table_name text,trigger_name text,function_name text,trigger_type integer,attributes text[])
       JOIN pg_catalog.pg_trigger t ON t.tgrelid=to_regclass('public.'||e.table_name) AND t.tgname=e.trigger_name
       WHERE NOT t.tgisinternal AND t.tgenabled IN ('O','A') AND t.tgtype=e.trigger_type
       AND t.tgfoid=to_regprocedure(e.function_name) AND t.tgnargs=0 AND t.tgqual IS NULL
       AND coalesce((SELECT array_agg(a.attname::text ORDER BY a.attname)
          FROM unnest(t.tgattr::smallint[]) n(attnum) JOIN pg_catalog.pg_attribute a
          ON a.attrelid=t.tgrelid AND a.attnum=n.attnum),ARRAY[]::text[])=e.attributes)
      """), {'table_count': len(BILLING_TABLES), 'tables': list(BILLING_TABLES),
              'function_count': len(functions), 'functions': json.dumps(functions),
              'trigger_count': len(triggers), 'triggers': json.dumps(triggers)}) is True


def billing_enabled(connection: Connection) -> bool:
    if not billing_available(connection):
        return False
    return connection.scalar(text('SELECT public.kpdesk_billing_enabled()')) is True


_TABLE_DDL = (
    """CREATE TABLE public.billing_config (
      singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
      enabled boolean NOT NULL DEFAULT false)""",
    "INSERT INTO public.billing_config(singleton) VALUES(true)",
    """CREATE TABLE public.billing_plans (
      id text PRIMARY KEY CHECK(id IN ('start','team','business')),
      name text NOT NULL, price_minor bigint NOT NULL CHECK(price_minor>0),
      people_limit integer NOT NULL CHECK(people_limit>0),
      new_quotes integer NOT NULL CHECK(new_quotes>0), catalog_items integer NOT NULL CHECK(catalog_items>0),
      days integer NOT NULL DEFAULT 30 CHECK(days=30))""",
    """INSERT INTO public.billing_plans VALUES
      ('start','Старт',29900,1,30,500,30), ('team','Команда',49900,5,100,2000,30),
      ('business','Бизнес',99000,15,300,5000,30)""",
    """CREATE TABLE public.billing_offers (
      plan_id text PRIMARY KEY REFERENCES public.billing_plans(id) ON DELETE RESTRICT,
      product_id uuid NOT NULL, offer_id uuid NOT NULL)""",
    """CREATE TABLE public.billing_orders (
      order_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      organization_id uuid NOT NULL REFERENCES public.organizations(id) ON DELETE RESTRICT,
      created_by uuid NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
      buyer_email text NOT NULL CHECK(length(buyer_email) BETWEEN 3 AND 254),
      idempotency_key uuid NOT NULL, plan_id text NOT NULL REFERENCES public.billing_plans(id),
      amount_minor bigint NOT NULL CHECK(amount_minor>0), currency text NOT NULL CHECK(currency='RUB'),
      product_id uuid NOT NULL, offer_id uuid NOT NULL,
      status text NOT NULL CHECK(status IN ('creating','pending','ambiguous','failed','paid','refunded','chargeback')),
      invoice_id uuid UNIQUE, payment_url text CHECK(length(payment_url)<=2048),
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      UNIQUE(organization_id,idempotency_key))""",
    """CREATE UNIQUE INDEX billing_one_open_order ON public.billing_orders(organization_id)
      WHERE status IN ('creating','pending','ambiguous')""",
    """CREATE TABLE public.billing_events (
      event_id text PRIMARY KEY CHECK(event_id ~ '^[0-9a-f]{64}$'),
      order_id uuid NOT NULL REFERENCES public.billing_orders(order_id) ON DELETE RESTRICT,
      event_type text NOT NULL CHECK(length(event_type) BETWEEN 1 AND 100),
      invoice_id uuid NOT NULL,
      status text NOT NULL CHECK(status IN ('pending','paid','failed','refunded','chargeback')),
      amount_minor bigint NOT NULL, currency text NOT NULL,
      observed_at timestamptz NOT NULL DEFAULT clock_timestamp())""",
    """CREATE TABLE public.billing_grants (
      order_id uuid PRIMARY KEY REFERENCES public.billing_orders(order_id) ON DELETE RESTRICT,
      organization_id uuid NOT NULL REFERENCES public.organizations(id) ON DELETE RESTRICT,
      plan_id text NOT NULL REFERENCES public.billing_plans(id),
      starts_at timestamptz NOT NULL, ends_at timestamptz NOT NULL CHECK(ends_at>starts_at),
      revoked_at timestamptz, created_at timestamptz NOT NULL DEFAULT clock_timestamp())""",
    "CREATE INDEX billing_grant_period ON public.billing_grants(organization_id,starts_at,ends_at) WHERE revoked_at IS NULL",
    """CREATE TABLE public.billing_reversals (
      event_id uuid PRIMARY KEY,
      event_type text NOT NULL CHECK(event_type IN ('refund.success','chargeback.initiated')),
      product_id uuid NOT NULL, offer_id uuid NOT NULL,
      buyer_email text NOT NULL CHECK(length(buyer_email) BETWEEN 3 AND 254 AND buyer_email=lower(btrim(buyer_email))),
      amount_minor bigint NOT NULL CHECK(amount_minor>0), currency text NOT NULL CHECK(currency='RUB'),
      received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      resolved_order_id uuid REFERENCES public.billing_orders(order_id) ON DELETE RESTRICT,
      resolved_at timestamptz,
      CHECK((resolved_order_id IS NULL)=(resolved_at IS NULL)))""",

    """CREATE TABLE public.billing_usage (
      organization_id uuid NOT NULL REFERENCES public.organizations(id),
      kind text NOT NULL CHECK(kind IN ('universal_quote','typed_quote')),
      resource_id uuid NOT NULL, order_id uuid NOT NULL REFERENCES public.billing_grants(order_id),
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      PRIMARY KEY(organization_id,kind,resource_id))""",
)

_ENABLED = """CREATE FUNCTION public.kpdesk_billing_enabled() RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT coalesce((SELECT enabled FROM public.billing_config WHERE singleton),false)
$$"""

_LOCK = """CREATE FUNCTION public.kpdesk_billing_lock(p_org uuid) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 -- Match legacy email invitations: organization row lock precedes billing lock.
 PERFORM 1 FROM public.organizations WHERE id=p_org FOR KEY SHARE;
 PERFORM pg_advisory_xact_lock(hashtextextended('kpdesk:billing:'||p_org::text,0));
END $$"""

_ACTOR = """CREATE FUNCTION public.kpdesk_billing_actor(p_org uuid,p_action text) RETURNS uuid
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT s.user_id FROM public.auth_sessions s JOIN public.users u ON u.id=s.user_id
 WHERE s.token_sha256=current_setting('kpdesk.session_sha256',true)
 AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp()
 AND u.status='active' AND u.email_verified_at IS NOT NULL
 AND p_action IN ('read','manage') AND public.kpdesk_session_tenant_allowed(p_org,p_action)
$$"""

_CONFIGURE = """CREATE FUNCTION public.kpdesk_billing_configure(p_enabled boolean,p_offers jsonb) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE item record;
BEGIN
 IF p_enabled IS NULL OR jsonb_typeof(p_offers) IS DISTINCT FROM 'object'
 OR (p_offers-ARRAY['start','team','business'])<>'{}'::jsonb THEN
   RAISE EXCEPTION 'kpdesk_billing_unavailable'; END IF;
 -- Table/owner role is the authority; a tenant-controlled setting cannot activate billing.
 UPDATE public.billing_config SET enabled=false WHERE singleton;
 FOR item IN SELECT key,value FROM jsonb_each(p_offers) LOOP
   IF jsonb_typeof(item.value) IS DISTINCT FROM 'object'
   OR (item.value-ARRAY['product_id','offer_id'])<>'{}'::jsonb
   OR (item.value->>'product_id') IS NULL OR (item.value->>'offer_id') IS NULL THEN
     RAISE EXCEPTION 'kpdesk_billing_unavailable'; END IF;
   INSERT INTO public.billing_offers(plan_id,product_id,offer_id)
   VALUES(item.key,(item.value->>'product_id')::uuid,(item.value->>'offer_id')::uuid)
   ON CONFLICT(plan_id) DO UPDATE SET product_id=excluded.product_id,offer_id=excluded.offer_id;
 END LOOP;
 IF p_enabled AND (SELECT count(*) FROM public.billing_offers)<>3 THEN
   RAISE EXCEPTION 'kpdesk_billing_unavailable'; END IF;
 UPDATE public.billing_config SET enabled=p_enabled WHERE singleton;
END $$"""

_ORDER = """CREATE FUNCTION public.kpdesk_billing_order(p_org uuid,p_id uuid) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 IF public.kpdesk_billing_actor(p_org,'manage') IS NULL THEN
   RETURN jsonb_build_object('error','forbidden'); END IF;
 RETURN (SELECT to_jsonb(o)-'idempotency_key' FROM public.billing_orders o
         WHERE o.organization_id=p_org AND o.order_id=p_id);
END $$"""

_SUMMARY = """CREATE FUNCTION public.kpdesk_billing_summary(p_org uuid) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE active public.billing_grants%ROWTYPE; quotes integer; products integer; members integer;
BEGIN
 IF public.kpdesk_billing_actor(p_org,'read') IS NULL THEN
   RETURN jsonb_build_object('error','forbidden'); END IF;
 SELECT * INTO active FROM public.billing_grants WHERE organization_id=p_org
 AND revoked_at IS NULL AND starts_at<=clock_timestamp() AND ends_at>clock_timestamp()
 ORDER BY starts_at,order_id LIMIT 1;
 SELECT count(*) INTO quotes FROM public.billing_usage WHERE order_id=active.order_id;
 SELECT greatest(coalesce((SELECT jsonb_array_length(payload->'products') FROM public.universal_catalogs
   WHERE organization_id=p_org ORDER BY created_at DESC,id DESC LIMIT 1),0),
   coalesce((SELECT rows_count FROM public.catalog_versions WHERE organization_id=p_org
   ORDER BY created_at DESC,id DESC LIMIT 1),0)) INTO products;
 SELECT count(*) INTO members FROM public.organization_memberships WHERE organization_id=p_org AND status='active';
 RETURN jsonb_build_object('available',true,'enabled',public.kpdesk_billing_enabled(),
   'plans',(SELECT jsonb_agg(to_jsonb(p) ORDER BY p.price_minor) FROM public.billing_plans p),
   'subscription',CASE WHEN active.order_id IS NULL THEN NULL ELSE jsonb_build_object(
     'plan_id',active.plan_id,'starts_at',active.starts_at,'ends_at',active.ends_at) END,
   'usage',jsonb_build_object('new_quotes',quotes,'catalog_items',products,'members',members),
   'orders',coalesce((SELECT jsonb_agg(safe ORDER BY created_at DESC,order_id DESC) FROM (
     SELECT o.created_at,o.order_id,jsonb_build_object('order_id',o.order_id,'plan_id',o.plan_id,
       'status',CASE WHEN o.status IN ('refunded','chargeback') THEN 'revoked' ELSE o.status END,
       'amount_minor',o.amount_minor,'currency',o.currency,'payment_url',o.payment_url,
       'created_at',o.created_at) AS safe FROM public.billing_orders o
     WHERE o.organization_id=p_org ORDER BY o.created_at DESC,o.order_id DESC LIMIT 30) recent),'[]'::jsonb));
END $$"""

_PREPARE = """CREATE FUNCTION public.kpdesk_billing_prepare(p_org uuid,p_plan text,p_key uuid) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE actor uuid; row public.billing_orders%ROWTYPE; plan public.billing_plans%ROWTYPE;
 offer public.billing_offers%ROWTYPE; email text;
BEGIN
 actor:=public.kpdesk_billing_actor(p_org,'manage');
 IF actor IS NULL THEN RETURN jsonb_build_object('error','forbidden'); END IF;
 PERFORM public.kpdesk_billing_lock(p_org);
 IF public.kpdesk_billing_actor(p_org,'manage') IS DISTINCT FROM actor THEN
   RETURN jsonb_build_object('error','forbidden'); END IF;
 IF NOT public.kpdesk_billing_enabled() THEN RETURN jsonb_build_object('error','unavailable'); END IF;
 IF p_key IS NULL OR p_plan IS NULL THEN RETURN jsonb_build_object('error','conflict'); END IF;
 SELECT * INTO row FROM public.billing_orders WHERE organization_id=p_org AND idempotency_key=p_key;
 IF FOUND THEN
   IF row.created_by<>actor OR row.plan_id<>p_plan THEN RETURN jsonb_build_object('error','conflict'); END IF;
   RETURN (to_jsonb(row)-'idempotency_key')||jsonb_build_object('created_new',false);
 END IF;
 SELECT * INTO plan FROM public.billing_plans WHERE id=p_plan;
 SELECT * INTO offer FROM public.billing_offers WHERE plan_id=p_plan;
 IF plan.id IS NULL OR offer.plan_id IS NULL THEN RETURN jsonb_build_object('error','unavailable'); END IF;
 IF EXISTS(SELECT 1 FROM public.billing_grants WHERE organization_id=p_org AND revoked_at IS NULL
     AND ends_at>clock_timestamp() AND plan_id<>p_plan)
 OR EXISTS(SELECT 1 FROM public.billing_orders WHERE organization_id=p_org
     AND status IN ('creating','pending','ambiguous')) THEN RETURN jsonb_build_object('error','conflict'); END IF;
 SELECT u.email INTO email FROM public.users u WHERE u.id=actor;
 INSERT INTO public.billing_orders(organization_id,created_by,buyer_email,idempotency_key,
   plan_id,amount_minor,currency,product_id,offer_id,status)
 VALUES(p_org,actor,email,p_key,p_plan,plan.price_minor,'RUB',offer.product_id,offer.offer_id,'creating')
 RETURNING * INTO row;
 RETURN (to_jsonb(row)-'idempotency_key')||jsonb_build_object('created_new',true);
END $$"""

_ATTACH = """CREATE FUNCTION public.kpdesk_billing_attach(p_org uuid,p_id uuid,p_invoice uuid,p_url text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE row public.billing_orders%ROWTYPE;
BEGIN
 IF public.kpdesk_billing_actor(p_org,'manage') IS NULL THEN RETURN jsonb_build_object('error','forbidden'); END IF;
 PERFORM public.kpdesk_billing_lock(p_org);
 IF public.kpdesk_billing_actor(p_org,'manage') IS NULL THEN RETURN jsonb_build_object('error','forbidden'); END IF;
 SELECT * INTO row FROM public.billing_orders WHERE organization_id=p_org AND order_id=p_id FOR UPDATE;
 IF NOT FOUND OR p_invoice IS NULL OR p_url IS NULL OR length(p_url) NOT BETWEEN 1 AND 2048
 OR (row.invoice_id IS NOT NULL AND row.invoice_id<>p_invoice) THEN RETURN jsonb_build_object('error','conflict'); END IF;
 UPDATE public.billing_orders SET invoice_id=p_invoice,payment_url=p_url,
   status=CASE WHEN status IN ('creating','ambiguous') THEN 'pending' ELSE status END
 WHERE order_id=p_id RETURNING * INTO row;
 RETURN to_jsonb(row)-'idempotency_key';
END $$"""

_MARK = """CREATE FUNCTION public.kpdesk_billing_mark(p_org uuid,p_id uuid,p_status text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE row public.billing_orders%ROWTYPE;
BEGIN
 IF public.kpdesk_billing_actor(p_org,'manage') IS NULL THEN RETURN jsonb_build_object('error','forbidden'); END IF;
 PERFORM public.kpdesk_billing_lock(p_org);
 IF public.kpdesk_billing_actor(p_org,'manage') IS NULL THEN RETURN jsonb_build_object('error','forbidden'); END IF;
 IF p_status IS NULL OR p_status NOT IN ('ambiguous','failed') THEN RETURN jsonb_build_object('error','conflict'); END IF;
 UPDATE public.billing_orders SET status=CASE WHEN status='creating' THEN p_status ELSE status END
 WHERE organization_id=p_org AND order_id=p_id RETURNING * INTO row;
 RETURN CASE WHEN row.order_id IS NULL THEN NULL ELSE to_jsonb(row)-'idempotency_key' END;
END $$"""

_LOOKUP = """CREATE FUNCTION public.kpdesk_billing_verified_lookup(p_invoice uuid,p_correlation uuid) RETURNS jsonb
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT to_jsonb(o)-'idempotency_key' FROM public.billing_orders o
 WHERE (p_correlation IS NOT NULL AND o.order_id=p_correlation
        AND (o.invoice_id IS NULL OR p_invoice IS NULL OR o.invoice_id=p_invoice))
 OR (p_correlation IS NULL AND p_invoice IS NOT NULL AND o.invoice_id=p_invoice)
$$"""

_SETTLE = """CREATE FUNCTION public.kpdesk_billing_settle(p_id uuid,p_event text,p_type text,p_invoice uuid,
 p_status text,p_amount bigint,p_currency text,p_product uuid,p_offer uuid,p_email text,
 p_correlation uuid,p_url text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE row public.billing_orders%ROWTYPE; grant_row public.billing_grants%ROWTYPE;
 now_at timestamptz; next_at timestamptz; old_event public.billing_events%ROWTYPE;
BEGIN
 SELECT * INTO row FROM public.billing_orders WHERE order_id=p_id;
 IF NOT FOUND THEN RETURN jsonb_build_object('error','mismatch'); END IF;
 PERFORM public.kpdesk_billing_lock(row.organization_id);
 SELECT * INTO row FROM public.billing_orders WHERE order_id=p_id FOR UPDATE;
 IF p_event IS NULL OR p_event !~ '^[0-9a-f]{64}$' OR p_type IS NULL
 OR length(p_type) NOT BETWEEN 1 AND 100 OR p_invoice IS NULL
 OR p_status IS NULL OR p_status NOT IN ('pending','paid','failed','refunded','chargeback')
 OR p_amount IS DISTINCT FROM row.amount_minor OR p_currency IS DISTINCT FROM row.currency
 OR (p_product IS NOT NULL AND p_product<>row.product_id)
 OR (p_offer IS NOT NULL AND p_offer<>row.offer_id)
 OR p_email IS DISTINCT FROM row.buyer_email OR p_correlation IS DISTINCT FROM row.order_id
 OR (row.invoice_id IS NOT NULL AND p_invoice<>row.invoice_id)
 OR (p_url IS NOT NULL AND length(p_url) NOT BETWEEN 1 AND 2048)
 THEN RETURN jsonb_build_object('error','mismatch'); END IF;
 SELECT * INTO old_event FROM public.billing_events WHERE event_id=p_event;
 IF FOUND THEN
   IF old_event.order_id<>p_id OR old_event.status<>p_status OR old_event.invoice_id<>p_invoice
   OR old_event.event_type<>p_type THEN RETURN jsonb_build_object('error','mismatch'); END IF;
   RETURN (to_jsonb(row)-'idempotency_key')||jsonb_build_object('duplicate',true);
 END IF;
 -- Verified readback may arrive before the HTTP create response is attached.
 UPDATE public.billing_orders SET invoice_id=p_invoice,
   payment_url=coalesce(p_url,payment_url) WHERE order_id=p_id;
 INSERT INTO public.billing_events(event_id,order_id,event_type,invoice_id,status,amount_minor,currency)
 VALUES(p_event,p_id,p_type,p_invoice,p_status,p_amount,p_currency);
 now_at:=clock_timestamp();
 IF p_status='paid' AND row.status NOT IN ('refunded','chargeback') THEN
   IF NOT EXISTS(SELECT 1 FROM public.billing_grants WHERE order_id=p_id) THEN
     SELECT greatest(now_at,coalesce(max(ends_at),now_at)) INTO next_at FROM public.billing_grants
     WHERE organization_id=row.organization_id AND revoked_at IS NULL;
     INSERT INTO public.billing_grants(order_id,organization_id,plan_id,starts_at,ends_at)
     VALUES(p_id,row.organization_id,row.plan_id,next_at,next_at+interval '30 days');
   END IF;
   UPDATE public.billing_orders SET status='paid' WHERE order_id=p_id;
 ELSIF p_status IN ('refunded','chargeback') THEN
   UPDATE public.billing_orders SET status=p_status WHERE order_id=p_id;
   UPDATE public.billing_grants SET revoked_at=coalesce(revoked_at,now_at) WHERE order_id=p_id;
   -- Period rows are a projection. The payment/event ledger remains append-only.
   -- Only untouched future grants move forward; each retains all paid 30 days.
   SELECT greatest(now_at,coalesce(max(ends_at),now_at)) INTO next_at FROM public.billing_grants
   WHERE organization_id=row.organization_id AND revoked_at IS NULL AND starts_at<now_at;
   FOR grant_row IN SELECT * FROM public.billing_grants WHERE organization_id=row.organization_id
       AND revoked_at IS NULL AND starts_at>=now_at ORDER BY starts_at,created_at,order_id LOOP
     UPDATE public.billing_grants SET starts_at=next_at,ends_at=next_at+interval '30 days'
       WHERE order_id=grant_row.order_id;
     next_at:=next_at+interval '30 days';
   END LOOP;
 ELSIF row.status NOT IN ('paid','refunded','chargeback')
   AND NOT (row.status='failed' AND p_status='pending') THEN
   UPDATE public.billing_orders SET status=p_status WHERE order_id=p_id;
 END IF;
 SELECT * INTO row FROM public.billing_orders WHERE order_id=p_id;
 RETURN (to_jsonb(row)-'idempotency_key')||jsonb_build_object('duplicate',false);
END $$"""

_RECORD_REVERSAL = """CREATE FUNCTION public.kpdesk_billing_record_reversal(p_event uuid,p_type text,
 p_product uuid,p_offer uuid,p_email text,p_amount bigint,p_currency text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE result public.billing_reversals%ROWTYPE;
BEGIN
 IF p_event IS NULL OR p_type IS NULL OR p_type NOT IN ('refund.success','chargeback.initiated')
 OR p_product IS NULL OR p_offer IS NULL OR p_email IS NULL OR length(p_email) NOT BETWEEN 3 AND 254
 OR p_email<>lower(btrim(p_email)) OR p_amount IS NULL OR p_amount<=0
 OR p_currency IS DISTINCT FROM 'RUB' THEN RETURN jsonb_build_object('error','mismatch'); END IF;
 INSERT INTO public.billing_reversals(event_id,event_type,product_id,offer_id,buyer_email,amount_minor,currency)
 VALUES(p_event,p_type,p_product,p_offer,p_email,p_amount,p_currency) ON CONFLICT DO NOTHING;
 SELECT * INTO result FROM public.billing_reversals WHERE event_id=p_event FOR UPDATE;
 IF result.event_type<>p_type OR result.product_id<>p_product OR result.offer_id<>p_offer
 OR result.buyer_email<>p_email OR result.amount_minor<>p_amount OR result.currency<>p_currency
 THEN RETURN jsonb_build_object('error','mismatch'); END IF;
 RETURN to_jsonb(result);
END $$"""

_GET_REVERSAL = """CREATE FUNCTION public.kpdesk_billing_get_reversal(p_event uuid) RETURNS jsonb
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT to_jsonb(r) FROM public.billing_reversals r WHERE event_id=p_event
$$"""

_RESOLVE_REVERSAL = """CREATE FUNCTION public.kpdesk_billing_resolve_reversal(p_event uuid,p_order uuid,
 p_invoice uuid,p_amount bigint,p_currency text,p_product uuid,p_offer uuid,p_email text,
 p_correlation uuid,p_url text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE reversal public.billing_reversals%ROWTYPE; original public.billing_orders%ROWTYPE;
 result jsonb; reversal_status text; fingerprint text;
BEGIN
 -- This inbox lock always precedes the billing org lock. No other gateway
 -- acquires an inbox row after holding a billing lock.
 SELECT * INTO reversal FROM public.billing_reversals WHERE event_id=p_event FOR UPDATE;
 IF NOT FOUND THEN RETURN jsonb_build_object('error','mismatch'); END IF;
 SELECT * INTO original FROM public.billing_orders WHERE order_id=p_order;
 IF NOT FOUND OR p_correlation IS DISTINCT FROM p_order OR p_invoice IS NULL
 OR (original.invoice_id IS NOT NULL AND original.invoice_id<>p_invoice)
 OR original.amount_minor IS DISTINCT FROM p_amount OR original.currency IS DISTINCT FROM p_currency
 OR original.buyer_email IS DISTINCT FROM p_email
 OR reversal.product_id<>original.product_id OR reversal.offer_id<>original.offer_id
 OR reversal.buyer_email<>original.buyer_email OR reversal.currency<>original.currency
 OR reversal.amount_minor>original.amount_minor
 OR (p_product IS NOT NULL AND p_product<>original.product_id)
 OR (p_offer IS NOT NULL AND p_offer<>original.offer_id)
 OR (reversal.resolved_order_id IS NOT NULL AND reversal.resolved_order_id<>p_order)
 THEN RETURN jsonb_build_object('error','mismatch'); END IF;
 reversal_status:=CASE WHEN reversal.event_type='refund.success' THEN 'refunded' ELSE 'chargeback' END;
 fingerprint:=encode(sha256(convert_to('kpdesk:reversal:'||p_event::text,'UTF8')),'hex');
 -- Uses the original receipt amount. Partial reversal evidence remains immutable
 -- in the inbox and is never substituted for the original order amount.
 result:=public.kpdesk_billing_settle(p_order,fingerprint,reversal.event_type,p_invoice,
   reversal_status,p_amount,p_currency,p_product,p_offer,p_email,p_correlation,p_url);
 IF result IS NULL OR result ? 'error' THEN RETURN jsonb_build_object('error','mismatch'); END IF;
 UPDATE public.billing_reversals SET resolved_order_id=p_order,
   resolved_at=coalesce(resolved_at,clock_timestamp()) WHERE event_id=p_event;
 RETURN result;
END $$"""

_ENTITLEMENT = """CREATE FUNCTION public.kpdesk_billing_entitlement(p_org uuid) RETURNS public.billing_grants
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE result public.billing_grants%ROWTYPE;
BEGIN
 PERFORM public.kpdesk_billing_lock(p_org);
 SELECT * INTO result FROM public.billing_grants WHERE organization_id=p_org
   AND revoked_at IS NULL AND starts_at<=clock_timestamp() AND ends_at>clock_timestamp()
   ORDER BY starts_at,order_id LIMIT 1;
 IF result.order_id IS NULL THEN RAISE EXCEPTION 'kpdesk_billing_payment_required'; END IF;
 RETURN result;
END $$"""

_SEATS = """CREATE FUNCTION public.kpdesk_billing_seats(p_org uuid,p_exclude text) RETURNS integer
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT count(*)::integer FROM (
   SELECT lower(u.email) AS email FROM public.organization_memberships m
     JOIN public.users u ON u.id=m.user_id WHERE m.organization_id=p_org AND m.status='active'
   UNION SELECT lower(u.email) FROM public.organization_invitations i
     JOIN public.users u ON u.id=i.user_id WHERE i.organization_id=p_org
     AND i.status='pending' AND i.expires_at>clock_timestamp()
   UNION SELECT lower(i.email) FROM public.organization_email_invitations i WHERE i.organization_id=p_org
     AND i.status IN ('pending','sending','sent') AND i.consumed_at IS NULL AND i.expires_at>clock_timestamp()
 ) occupied WHERE p_exclude IS NULL OR email<>lower(p_exclude)
$$"""

_QUOTA = """CREATE FUNCTION public.kpdesk_billing_quota_guard() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE grant_row public.billing_grants%ROWTYPE; plan public.billing_plans%ROWTYPE;
 item_id uuid; usage_kind text; recipient text; used integer;
BEGIN
 IF NOT public.kpdesk_billing_enabled() OR NEW.organization_id IS NULL THEN RETURN NEW; END IF;
 -- All growth paths share one org lock. Queries below never lock invitation rows,
 -- avoiding inversion against invitation acceptance's existing row locks.
 IF TG_TABLE_NAME='universal_quote_revisions' THEN
   PERFORM public.kpdesk_billing_lock(NEW.organization_id);
   IF EXISTS(SELECT 1 FROM public.universal_quote_revisions q
     WHERE q.organization_id=NEW.organization_id AND q.quote_id=NEW.quote_id
       AND (q.created_at,q.revision)<(NEW.created_at,NEW.revision)) THEN RETURN NEW; END IF;
 END IF;
 IF TG_TABLE_NAME='organization_memberships' THEN
   IF NEW.status<>'active' THEN RETURN NEW; END IF;
   IF TG_OP='UPDATE' THEN
     IF OLD.status='active' AND OLD.organization_id=NEW.organization_id AND OLD.user_id=NEW.user_id THEN RETURN NEW; END IF;
   END IF;
   PERFORM public.kpdesk_billing_lock(NEW.organization_id);
   -- Account onboarding may create exactly the first owner before a purchase.
   IF NEW.role='owner' AND NOT EXISTS(SELECT 1 FROM public.organization_memberships
       WHERE organization_id=NEW.organization_id) THEN RETURN NEW; END IF;
 ELSIF TG_TABLE_NAME='organization_invitations' THEN
   IF NEW.status<>'pending' OR NEW.expires_at<=clock_timestamp() THEN RETURN NEW; END IF;
   IF TG_OP='UPDATE' THEN
     IF OLD.status='pending' AND OLD.expires_at>clock_timestamp()
       AND OLD.organization_id=NEW.organization_id AND OLD.user_id=NEW.user_id THEN RETURN NEW; END IF;
   END IF;
 ELSIF TG_TABLE_NAME='organization_email_invitations' THEN
   IF NEW.status NOT IN ('pending','sending','sent') OR NEW.consumed_at IS NOT NULL
     OR NEW.expires_at<=clock_timestamp() THEN RETURN NEW; END IF;
   IF TG_OP='UPDATE' THEN
     IF OLD.status IN ('pending','sending','sent') AND OLD.consumed_at IS NULL
       AND OLD.expires_at>clock_timestamp() AND OLD.organization_id=NEW.organization_id
       AND OLD.email=NEW.email THEN RETURN NEW; END IF;
   END IF;
 END IF;
 grant_row:=public.kpdesk_billing_entitlement(NEW.organization_id);
 SELECT * INTO plan FROM public.billing_plans WHERE id=grant_row.plan_id;
 IF TG_TABLE_NAME='universal_catalogs' THEN
   IF jsonb_typeof(NEW.payload->'products') IS DISTINCT FROM 'array'
     OR jsonb_array_length(NEW.payload->'products')>plan.catalog_items THEN
     RAISE EXCEPTION 'kpdesk_billing_catalog_limit'; END IF;
 ELSIF TG_TABLE_NAME='catalog_versions' THEN
   IF NEW.rows_count>plan.catalog_items THEN RAISE EXCEPTION 'kpdesk_billing_catalog_limit'; END IF;
 ELSIF TG_TABLE_NAME IN ('universal_quote_revisions','quote_documents') THEN
   IF TG_TABLE_NAME='quote_documents' THEN item_id:=NEW.id; usage_kind:='typed_quote';
   ELSE item_id:=NEW.quote_id; usage_kind:='universal_quote'; END IF;
   IF EXISTS(SELECT 1 FROM public.billing_usage WHERE organization_id=NEW.organization_id
     AND billing_usage.kind=usage_kind AND resource_id=item_id) THEN RETURN NEW; END IF;
   SELECT count(*) INTO used FROM public.billing_usage WHERE order_id=grant_row.order_id;
   IF used>=plan.new_quotes THEN RAISE EXCEPTION 'kpdesk_billing_quote_limit'; END IF;
   INSERT INTO public.billing_usage(organization_id,kind,resource_id,order_id)
     VALUES(NEW.organization_id,usage_kind,item_id,grant_row.order_id);
 ELSIF TG_TABLE_NAME IN ('organization_memberships','organization_invitations','organization_email_invitations') THEN
   IF TG_TABLE_NAME='organization_email_invitations' THEN recipient:=NEW.email;
   ELSE SELECT email INTO recipient FROM public.users WHERE id=NEW.user_id; END IF;
   IF public.kpdesk_billing_seats(NEW.organization_id,recipient)>=plan.people_limit THEN
     RAISE EXCEPTION 'kpdesk_billing_member_limit'; END IF;
 END IF;
 RETURN NEW;
END $$"""

_IMMUTABLE = """CREATE FUNCTION public.kpdesk_billing_immutable() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 IF TG_TABLE_NAME='billing_reversals' AND TG_OP='UPDATE' THEN
   IF (to_jsonb(NEW)-ARRAY['resolved_order_id','resolved_at'])=
      (to_jsonb(OLD)-ARRAY['resolved_order_id','resolved_at'])
      AND NEW.resolved_order_id IS NOT NULL AND NEW.resolved_at IS NOT NULL
      AND (OLD.resolved_order_id IS NULL OR (NEW.resolved_order_id=OLD.resolved_order_id
           AND NEW.resolved_at=OLD.resolved_at)) THEN RETURN NEW; END IF;
 END IF;
 IF TG_TABLE_NAME='billing_orders' AND TG_OP='UPDATE' THEN
   IF (to_jsonb(NEW)-ARRAY['status','invoice_id','payment_url'])=
      (to_jsonb(OLD)-ARRAY['status','invoice_id','payment_url'])
      AND (OLD.invoice_id IS NULL OR NEW.invoice_id IS NOT DISTINCT FROM OLD.invoice_id)
   THEN RETURN NEW; END IF;
 END IF;
 RAISE EXCEPTION 'kpdesk_billing_order_conflict';
END $$"""

# No runtime role can invoke helper/configuration functions directly.
_FUNCTIONS = (
    ('kpdesk_billing_enabled()', _ENABLED, ('kpdesk_tenant_runtime', 'kpdesk_auth_runtime')),
    ('kpdesk_billing_lock(uuid)', _LOCK, ()),
    ('kpdesk_billing_actor(uuid,text)', _ACTOR, ()),
    ('kpdesk_billing_configure(boolean,jsonb)', _CONFIGURE, ()),
    ('kpdesk_billing_order(uuid,uuid)', _ORDER, ('kpdesk_tenant_runtime',)),
    ('kpdesk_billing_summary(uuid)', _SUMMARY, ('kpdesk_tenant_runtime',)),
    ('kpdesk_billing_prepare(uuid,text,uuid)', _PREPARE, ('kpdesk_tenant_runtime',)),
    ('kpdesk_billing_attach(uuid,uuid,uuid,text)', _ATTACH, ('kpdesk_tenant_runtime',)),
    ('kpdesk_billing_mark(uuid,uuid,text)', _MARK, ('kpdesk_tenant_runtime',)),
    ('kpdesk_billing_verified_lookup(uuid,uuid)', _LOOKUP, ('kpdesk_auth_runtime',)),
    ('kpdesk_billing_settle(uuid,text,text,uuid,text,bigint,text,uuid,uuid,text,uuid,text)', _SETTLE, ('kpdesk_auth_runtime',)),
    ('kpdesk_billing_record_reversal(uuid,text,uuid,uuid,text,bigint,text)', _RECORD_REVERSAL, ('kpdesk_auth_runtime',)),
    ('kpdesk_billing_get_reversal(uuid)', _GET_REVERSAL, ('kpdesk_auth_runtime',)),
    ('kpdesk_billing_resolve_reversal(uuid,uuid,uuid,bigint,text,uuid,uuid,text,uuid,text)', _RESOLVE_REVERSAL, ('kpdesk_auth_runtime',)),
    ('kpdesk_billing_entitlement(uuid)', _ENTITLEMENT, ()),
    ('kpdesk_billing_seats(uuid,text)', _SEATS, ()),
    ('kpdesk_billing_quota_guard()', _QUOTA, ()),
    ('kpdesk_billing_immutable()', _IMMUTABLE, ()),
)


_IMMUTABLE_TABLES = ('billing_orders', 'billing_events', 'billing_usage', 'billing_reversals')
_QUOTA_TRIGGERS: tuple[tuple[str, str, str, int, list[str]], ...] = (
    ('universal_catalogs', 'BEFORE', 'INSERT OR UPDATE OF payload,organization_id', 23, ['organization_id', 'payload']),
    ('catalog_versions', 'BEFORE', 'INSERT OR UPDATE OF rows_count,organization_id', 23, ['organization_id', 'rows_count']),
    ('universal_quote_revisions', 'AFTER', 'INSERT', 5, []),
    ('quote_documents', 'AFTER', 'INSERT', 5, []),
    ('organization_memberships', 'BEFORE', 'INSERT OR UPDATE', 23, []),
    ('organization_invitations', 'BEFORE', 'INSERT OR UPDATE', 23, []),
    ('organization_email_invitations', 'BEFORE', 'INSERT OR UPDATE', 23, []),
    ('provider_budget_operations', 'BEFORE', 'INSERT', 7, []),
    ('provider_budget_attempts', 'BEFORE', 'INSERT', 7, []),
)


def create_billing_schema(connection: Connection) -> None:
    """DDL primitive called by approved 0048 only, never an application bootstrap."""
    for statement in _TABLE_DDL:
        connection.execute(text(statement))
    for table in BILLING_TABLES:
        for statement in (
            f'REVOKE ALL ON public.{table} FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime',
            f'ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY',
            f'ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY',
            f'CREATE POLICY {table}_owner ON public.{table} TO kpdesk USING(true) WITH CHECK(true)',
        ):
            connection.execute(text(statement))
    for signature, definition, roles in _FUNCTIONS:
        connection.execute(text(definition))
        connection.execute(text(f'REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime'))
        for role in roles:
            connection.execute(text(f'GRANT EXECUTE ON FUNCTION public.{signature} TO {role}'))
    for table in _IMMUTABLE_TABLES:
        connection.execute(text(f'''CREATE TRIGGER billing_immutable BEFORE UPDATE OR DELETE
            ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.kpdesk_billing_immutable()'''))
    # AFTER INSERT avoids charging ON CONFLICT DO NOTHING replays.
    for table, timing, operation, _trigger_type, _attributes in _QUOTA_TRIGGERS:
        connection.execute(text(f'''CREATE TRIGGER billing_quota_guard {timing} {operation}
            ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.kpdesk_billing_quota_guard()'''))
