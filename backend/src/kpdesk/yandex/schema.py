"""Approved 0049 installation and read-only Yandex security readiness checks."""
from __future__ import annotations

import json

from sqlalchemy import Connection, text

YANDEX_TABLES = ('yandex_oauth_flows', 'yandex_identities')


def yandex_available(connection: Connection) -> bool:
    """Inspect catalogs only; never mutate a flow or impersonate an account."""
    functions = [{'signature': 'public.' + signature, 'roles': list(roles)}
                 for signature, _definition, roles in _FUNCTIONS]
    return connection.scalar(text("""SELECT
      (SELECT count(*)=2 FROM pg_catalog.pg_class c
       WHERE c.relnamespace='public'::regnamespace AND c.relname=ANY(CAST(:tables AS text[]))
       AND c.relkind='r' AND c.relrowsecurity AND c.relforcerowsecurity
       AND pg_get_userbyid(c.relowner)='kpdesk'
       AND NOT EXISTS(SELECT 1 FROM aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) a
                      WHERE a.grantee<>c.relowner)
       AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_attribute att,
                      LATERAL aclexplode(att.attacl) a
                      WHERE att.attrelid=c.oid AND a.grantee<>c.relowner)
       AND NOT EXISTS(SELECT 1 FROM unnest(ARRAY['kpdesk_app','kpdesk_auth_runtime','kpdesk_tenant_runtime']) r(role)
           WHERE has_table_privilege(r.role,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
              OR has_any_column_privilege(r.role,c.oid,'SELECT,INSERT,UPDATE,REFERENCES'))
       AND (SELECT count(*)=1 FROM pg_catalog.pg_policy pol WHERE pol.polrelid=c.oid)
       AND EXISTS(SELECT 1 FROM pg_catalog.pg_policy pol WHERE pol.polrelid=c.oid
                  AND pol.polroles=ARRAY[c.relowner]::oid[] AND pol.polcmd='*' AND pol.polpermissive
                  AND pg_get_expr(pol.polqual,pol.polrelid)='true'
                  AND pg_get_expr(pol.polwithcheck,pol.polrelid)='true'))
      AND (SELECT count(*)=4 FROM jsonb_to_recordset(CAST(:functions AS jsonb))
          e(signature text,roles text[]) JOIN pg_catalog.pg_proc p ON p.oid=to_regprocedure(e.signature)
       WHERE p.prosecdef AND p.proconfig=ARRAY['search_path=pg_catalog, pg_temp']::text[]
       AND pg_get_userbyid(p.proowner)='kpdesk'
       AND NOT EXISTS(SELECT 1 FROM aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
          WHERE a.grantee<>p.proowner AND
                (a.grantee=0 OR NOT pg_get_userbyid(a.grantee)=ANY(e.roles) OR a.is_grantable))
       AND NOT EXISTS(SELECT 1 FROM unnest(ARRAY['kpdesk_app','kpdesk_auth_runtime','kpdesk_tenant_runtime']) r(role)
          WHERE has_function_privilege(r.role,p.oid,'EXECUTE') IS DISTINCT FROM (r.role=ANY(e.roles))))
      """), {'tables': list(YANDEX_TABLES), 'functions': json.dumps(functions)}) is True


_TABLE_DDL = (
    """CREATE TABLE public.yandex_oauth_flows (
      state_hash text PRIMARY KEY CHECK(state_hash ~ '^[0-9a-f]{64}$'),
      browser_hash text NOT NULL CHECK(browser_hash ~ '^[0-9a-f]{64}$'),
      verifier_cipher text CHECK(length(verifier_cipher) BETWEEN 80 AND 4096),
      claim_hash text CHECK(claim_hash ~ '^[0-9a-f]{64}$'),
      mode text NOT NULL CHECK(mode IN ('login','link')),
      bound_user_id uuid REFERENCES public.users(id) ON DELETE RESTRICT,
      bound_session_hash text REFERENCES public.auth_sessions(token_sha256) ON DELETE RESTRICT,
      next_path text NOT NULL CHECK(next_path IN ('/app','/admin')),
      peer text NOT NULL CHECK(length(peer) BETWEEN 1 AND 64),
      created_at timestamptz NOT NULL,
      expires_at timestamptz NOT NULL CHECK(expires_at=created_at+interval '10 minutes'),
      status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','exchanging','completed')),
      CHECK((mode='login' AND bound_user_id IS NULL AND bound_session_hash IS NULL)
         OR (mode='link' AND bound_user_id IS NOT NULL AND bound_session_hash IS NOT NULL)),
      CHECK((status='pending' AND claim_hash IS NULL AND verifier_cipher IS NOT NULL)
         OR (status='exchanging' AND claim_hash IS NOT NULL AND verifier_cipher IS NOT NULL)
         OR (status='completed' AND verifier_cipher IS NULL)))""",
    'CREATE INDEX yandex_flow_expiry ON public.yandex_oauth_flows(expires_at)',
    'CREATE INDEX yandex_flow_peer_created ON public.yandex_oauth_flows(peer,created_at)',
    """CREATE TABLE public.yandex_identities (
      client_id text NOT NULL CHECK(length(client_id) BETWEEN 1 AND 255),
      subject text NOT NULL CHECK(length(subject) BETWEEN 1 AND 255),
      user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      PRIMARY KEY(client_id,subject), UNIQUE(client_id,user_id))""",
)

_ISSUE = """CREATE FUNCTION public.kpdesk_yandex_issue(
 p_state text,p_browser text,p_cipher text,p_mode text,p_next text,p_peer text,
 p_session text,p_csrf text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE observed timestamptz; actor uuid; bound_session_expires timestamptz;
BEGIN
 IF p_state IS NULL OR p_state !~ '^[0-9a-f]{64}$'
 OR p_browser IS NULL OR p_browser !~ '^[0-9a-f]{64}$'
 OR p_cipher IS NULL OR length(p_cipher) NOT BETWEEN 80 AND 4096
 OR p_mode IS NULL OR p_mode NOT IN ('login','link')
 OR p_next IS NULL OR p_next NOT IN ('/app','/admin')
 OR p_peer IS NULL OR length(p_peer) NOT BETWEEN 1 AND 64
 OR (p_mode='login' AND (p_session IS NOT NULL OR p_csrf IS NOT NULL))
 OR (p_mode='link' AND (p_session IS NULL OR p_csrf IS NULL
     OR p_session !~ '^[0-9a-f]{64}$' OR p_csrf !~ '^[0-9a-f]{64}$')) THEN
   RETURN jsonb_build_object('error','invalid_request'); END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('kpdesk:yandex:peer:'||p_peer,0));
 observed := clock_timestamp();
 DELETE FROM public.yandex_oauth_flows WHERE state_hash IN (
   SELECT state_hash FROM public.yandex_oauth_flows
   WHERE expires_at<observed-interval '24 hours' ORDER BY expires_at
   LIMIT 500 FOR UPDATE SKIP LOCKED);
 IF (SELECT count(*) FROM public.yandex_oauth_flows
     WHERE peer=p_peer AND created_at>observed-interval '5 minutes')>=20 THEN
   RETURN jsonb_build_object('error','rate_limited'); END IF;
 IF p_mode='link' THEN
   SELECT s.user_id INTO actor FROM public.auth_sessions s WHERE s.token_sha256=p_session;
   IF actor IS NULL THEN RETURN jsonb_build_object('error','session_invalid'); END IF;
   PERFORM 1 FROM public.users u WHERE u.id=actor AND u.status='active'
     AND u.email_verified_at IS NOT NULL FOR UPDATE OF u;
   IF NOT FOUND THEN RETURN jsonb_build_object('error','session_invalid'); END IF;
   SELECT s.expires_at INTO bound_session_expires FROM public.auth_sessions s
     WHERE s.token_sha256=p_session AND s.user_id=actor
     AND s.csrf_sha256=p_csrf AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp()
     FOR UPDATE OF s;
   -- A lock-only holder can delay this query beyond the predicate's clock check.
   IF NOT FOUND OR bound_session_expires<=clock_timestamp() THEN
     RETURN jsonb_build_object('error','session_invalid'); END IF;
 END IF;
 INSERT INTO public.yandex_oauth_flows(state_hash,browser_hash,verifier_cipher,mode,
   bound_user_id,bound_session_hash,next_path,peer,created_at,expires_at)
 VALUES(p_state,p_browser,p_cipher,p_mode,actor,p_session,p_next,p_peer,
   observed,observed+interval '10 minutes') ON CONFLICT DO NOTHING;
 IF NOT FOUND THEN RETURN jsonb_build_object('error','flow_invalid'); END IF;
 RETURN '{}'::jsonb;
END $$"""

_CLAIM = """CREATE FUNCTION public.kpdesk_yandex_claim(p_state text,p_browser text,p_claim text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE f public.yandex_oauth_flows%ROWTYPE;
BEGIN
 IF p_state IS NULL OR p_state !~ '^[0-9a-f]{64}$'
 OR p_browser IS NULL OR p_browser !~ '^[0-9a-f]{64}$'
 OR p_claim IS NULL OR p_claim !~ '^[0-9a-f]{64}$' THEN
   RETURN jsonb_build_object('error','invalid_request'); END IF;
 SELECT * INTO f FROM public.yandex_oauth_flows
 WHERE state_hash=p_state AND browser_hash=p_browser AND status='pending' FOR UPDATE;
 IF NOT FOUND THEN RETURN jsonb_build_object('error','flow_invalid'); END IF;
 IF f.expires_at<=clock_timestamp() THEN
   UPDATE public.yandex_oauth_flows SET status='completed',verifier_cipher=NULL WHERE state_hash=p_state;
   RETURN jsonb_build_object('error','flow_invalid'); END IF;
 UPDATE public.yandex_oauth_flows SET status='exchanging',claim_hash=p_claim WHERE state_hash=p_state;
 RETURN jsonb_build_object('verifier_cipher',f.verifier_cipher,'mode',f.mode,
                          'next_path',f.next_path,'expires_at',f.expires_at);
END $$"""

_COMPLETE = r"""CREATE FUNCTION public.kpdesk_yandex_complete(
 p_state text,p_claim text,p_client text,p_subject text,p_email text,
 p_session text,p_csrf text,p_expires timestamptz) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE f public.yandex_oauth_flows%ROWTYPE; actor uuid; existing_actor uuid;
 new_user uuid; new_org uuid; created boolean := false; observed timestamptz;
 bound_session_expires timestamptz;
BEGIN
 IF p_state IS NULL OR p_state !~ '^[0-9a-f]{64}$'
 OR p_claim IS NULL OR p_claim !~ '^[0-9a-f]{64}$'
 OR p_client IS NULL OR length(p_client) NOT BETWEEN 1 AND 255
 OR p_subject IS NULL OR length(p_subject) NOT BETWEEN 1 AND 255
 OR p_session IS NULL OR p_session !~ '^[0-9a-f]{64}$'
 OR p_csrf IS NULL OR p_csrf !~ '^[0-9a-f]{64}$'
 OR p_expires IS NULL OR p_expires<=clock_timestamp()
 OR p_expires>clock_timestamp()+interval '7 days'
 OR (p_email IS NOT NULL AND (length(p_email) NOT BETWEEN 3 AND 254
     OR p_email<>lower(btrim(p_email))
     OR p_email !~ '^[a-z0-9][a-z0-9._%+\-]{0,63}@[a-z0-9.-]+\.[a-z]{2,30}$')) THEN
   RETURN jsonb_build_object('error','invalid_request'); END IF;
 SELECT * INTO f FROM public.yandex_oauth_flows
 WHERE state_hash=p_state AND claim_hash=p_claim AND status='exchanging' FOR UPDATE;
 IF NOT FOUND THEN RETURN jsonb_build_object('error','flow_invalid'); END IF;
 -- This update is outside the subtransaction below: domain failures consume the claim.
 UPDATE public.yandex_oauth_flows SET status='completed',verifier_cipher=NULL WHERE state_hash=p_state;
 IF f.expires_at<=clock_timestamp() THEN RETURN jsonb_build_object('error','flow_invalid'); END IF;
 -- Stable provider subject/client serializes first-login and explicit-link races.
 PERFORM pg_advisory_xact_lock(hashtextextended(
   'kpdesk:yandex:identity:'||length(p_client)::text||':'||p_client||':'||p_subject,0));
 BEGIN
   SELECT user_id INTO existing_actor FROM public.yandex_identities
     WHERE client_id=p_client AND subject=p_subject;
   IF f.mode='link' THEN
     actor := f.bound_user_id;
     PERFORM 1 FROM public.users u WHERE u.id=actor AND u.status='active'
       AND u.email_verified_at IS NOT NULL FOR UPDATE OF u;
     IF NOT FOUND THEN RETURN jsonb_build_object('error','session_invalid'); END IF;
     -- User lock precedes session lock, matching password reset's revoke ordering.
     SELECT s.expires_at INTO bound_session_expires FROM public.auth_sessions s
       WHERE s.token_sha256=f.bound_session_hash AND s.user_id=actor
       AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp() FOR UPDATE OF s;
     -- Recheck the captured deadline after waiting even if the tuple was not updated.
     IF NOT FOUND OR bound_session_expires<=clock_timestamp() THEN
       RETURN jsonb_build_object('error','session_invalid'); END IF;
     IF (existing_actor IS NOT NULL AND existing_actor<>actor)
       OR EXISTS(SELECT 1 FROM public.yandex_identities
          WHERE client_id=p_client AND user_id=actor AND subject<>p_subject) THEN
       RETURN jsonb_build_object('error','identity_conflict'); END IF;
     INSERT INTO public.yandex_identities(client_id,subject,user_id)
       VALUES(p_client,p_subject,actor) ON CONFLICT DO NOTHING;
   ELSIF existing_actor IS NOT NULL THEN
     actor := existing_actor;
     PERFORM 1 FROM public.users u WHERE u.id=actor AND u.status='active'
       AND u.email_verified_at IS NOT NULL FOR UPDATE OF u;
     IF NOT FOUND THEN RETURN jsonb_build_object('error','account_disabled'); END IF;
   ELSE
     IF p_email IS NULL THEN RETURN jsonb_build_object('error','email_required'); END IF;
     new_user := gen_random_uuid(); new_org := gen_random_uuid();
     INSERT INTO public.users(id,email,password_hash,status,email_verified_at)
       VALUES(new_user,p_email,NULL,'active',clock_timestamp()) ON CONFLICT DO NOTHING;
     IF NOT FOUND THEN RETURN jsonb_build_object('error','link_required'); END IF;
     INSERT INTO public.organizations(id,name,status) VALUES(new_org,'Моя компания','active');
     INSERT INTO public.organization_memberships(organization_id,user_id,role,status)
       VALUES(new_org,new_user,'owner','active');
     INSERT INTO public.yandex_identities(client_id,subject,user_id) VALUES(p_client,p_subject,new_user);
     actor := new_user; created := true;
   END IF;
   observed := clock_timestamp();
   IF f.expires_at<=observed THEN RAISE SQLSTATE 'P4901'; END IF;
   -- A late link expiry must roll back its identity insert, but consume the flow.
   IF f.mode='link' AND bound_session_expires<=observed THEN
     RAISE SQLSTATE 'P4902'; END IF;
   IF p_expires<=observed THEN
     -- Roll back any provisioning in this subtransaction, retaining the consumed flow.
     RAISE check_violation; END IF;
   INSERT INTO public.auth_sessions(token_sha256,user_id,csrf_sha256,created_at,expires_at)
     VALUES(p_session,actor,p_csrf,observed,p_expires);
   RETURN jsonb_build_object('user_id',actor,'created',created);
 EXCEPTION WHEN unique_violation THEN
   RETURN jsonb_build_object('error','identity_conflict');
 WHEN SQLSTATE 'P4901' THEN
   RETURN jsonb_build_object('error','flow_invalid');
 WHEN SQLSTATE 'P4902' THEN
   RETURN jsonb_build_object('error','session_invalid');
 WHEN check_violation THEN
   RETURN jsonb_build_object('error','invalid_request');
 END;
END $$"""

_IDENTITY = """CREATE FUNCTION public.kpdesk_yandex_identity(p_session text,p_client text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT coalesce(p_session ~ '^[0-9a-f]{64}$' AND length(p_client) BETWEEN 1 AND 255
 AND EXISTS(SELECT 1 FROM public.auth_sessions s JOIN public.users u ON u.id=s.user_id
   JOIN public.yandex_identities i ON i.user_id=u.id AND i.client_id=p_client
   WHERE s.token_sha256=p_session AND s.revoked_at IS NULL AND s.expires_at>statement_timestamp()
   AND u.status='active' AND u.email_verified_at IS NOT NULL),false)
$$"""

_FUNCTIONS = (
    ('kpdesk_yandex_issue(text,text,text,text,text,text,text,text)', _ISSUE, ('kpdesk_auth_runtime',)),
    ('kpdesk_yandex_claim(text,text,text)', _CLAIM, ('kpdesk_auth_runtime',)),
    ('kpdesk_yandex_complete(text,text,text,text,text,text,text,timestamptz)', _COMPLETE,
     ('kpdesk_auth_runtime',)),
    ('kpdesk_yandex_identity(text,text)', _IDENTITY, ('kpdesk_auth_runtime', 'kpdesk_tenant_runtime')),
)


def create_yandex_schema(connection: Connection) -> None:
    """DDL primitive invoked only by explicitly approved 0049 migration."""
    for statement in _TABLE_DDL:
        connection.execute(text(statement))
    for table in YANDEX_TABLES:
        for statement in (
            f'ALTER TABLE public.{table} OWNER TO kpdesk',
            f'REVOKE ALL ON public.{table} FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime',
            f'ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY',
            f'ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY',
            f'CREATE POLICY {table}_owner ON public.{table} TO kpdesk USING(true) WITH CHECK(true)',
        ):
            connection.execute(text(statement))
    for signature, definition, roles in _FUNCTIONS:
        connection.execute(text(definition))
        connection.execute(text(f'ALTER FUNCTION public.{signature} OWNER TO kpdesk'))
        connection.execute(text(f'REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime'))
        for role in roles:
            connection.execute(text(f'GRANT EXECUTE ON FUNCTION public.{signature} TO {role}'))
