"""OpenRouter raw-attempt accounting, installed only by approved migration 0042."""
from sqlalchemy import Connection, text


def create_provider_budget(connection: Connection) -> None:
    statements = [
        """CREATE TABLE public.provider_budget_config (
            singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
            rpm integer NOT NULL, daily integer NOT NULL,
            parallel integer NOT NULL, calls integer NOT NULL)""",
        "INSERT INTO public.provider_budget_config VALUES (true,20,1000,20,6)",
        """CREATE TABLE public.provider_budget_operations (
            organization_id uuid NOT NULL REFERENCES public.organizations(id),
            user_id uuid NOT NULL REFERENCES public.users(id), operation_id uuid NOT NULL,
            fingerprint text NOT NULL, claim uuid NOT NULL,
            status text NOT NULL CHECK(status IN ('running','completed','failed','ambiguous')),
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY(organization_id,user_id,operation_id))""",
        """CREATE TABLE public.provider_budget_attempts (
            id uuid PRIMARY KEY, organization_id uuid NOT NULL, user_id uuid NOT NULL,
            operation_id uuid NOT NULL, ordinal integer NOT NULL, input_chars integer NOT NULL,
            status text NOT NULL CHECK(status IN ('reserved','success','rejected','retryable',
                'invalid_output','ambiguous','cancelled')),
            created_at timestamptz NOT NULL, lease_expires_at timestamptz NOT NULL,
            finished_at timestamptz, http_status integer, elapsed_ms integer,
            prompt_tokens integer, completion_tokens integer, total_tokens integer,
            FOREIGN KEY(organization_id,user_id,operation_id) REFERENCES
              public.provider_budget_operations(organization_id,user_id,operation_id),
            UNIQUE(organization_id,user_id,operation_id,ordinal))""",
        "CREATE INDEX ix_provider_budget_time ON public.provider_budget_attempts(created_at)",
        """REVOKE ALL ON public.provider_budget_config,public.provider_budget_operations,
            public.provider_budget_attempts FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,
            kpdesk_tenant_runtime""",
        _ACTOR, _RESERVE, _FINISH, _CLOSE,
    ]
    for statement in statements:
        connection.execute(text(statement))
    for signature in SIGNATURES:
        connection.execute(text(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC"))
        connection.execute(text(f"GRANT EXECUTE ON FUNCTION public.{signature} TO kpdesk_tenant_runtime"))
    connection.execute(text("REVOKE ALL ON FUNCTION public.kpdesk_provider_actor(uuid) FROM PUBLIC"))


SIGNATURES = (
    "kpdesk_provider_reserve(uuid,uuid,text,uuid,uuid,integer,integer,integer,integer,integer)",
    "kpdesk_provider_finish(uuid,uuid,uuid,uuid,text,integer,integer,integer,integer,integer)",
    "kpdesk_provider_close(uuid,uuid,uuid,boolean,boolean)",
)

_ACTOR = """CREATE FUNCTION public.kpdesk_provider_actor(p_org uuid) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE actor uuid;
BEGIN
 IF NOT COALESCE(public.kpdesk_session_tenant_allowed(p_org,'write'),false) THEN
   RETURN NULL;
 END IF;
 SELECT s.user_id INTO actor FROM public.auth_sessions s
 JOIN public.users u ON u.id=s.user_id
 JOIN public.organization_memberships m ON m.user_id=s.user_id AND m.organization_id=p_org
 WHERE s.token_sha256=current_setting('kpdesk.session_sha256',true)
   AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp()
   AND u.status='active' AND u.email_verified_at IS NOT NULL
   AND m.status='active' AND m.role IN ('owner','editor');
 RETURN actor;
END $$"""

_RESERVE = """CREATE FUNCTION public.kpdesk_provider_reserve(
 p_org uuid,p_op uuid,p_fingerprint text,p_claim uuid,p_attempt uuid,p_chars integer,
 p_rpm integer,p_day integer,p_parallel integer,p_calls integer)
RETURNS TABLE(reason text,ordinal integer,retry_after integer)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE actor uuid; cfg public.provider_budget_config%ROWTYPE;
 op public.provider_budget_operations%ROWTYPE; now_at timestamptz; n integer;
BEGIN
 IF p_org IS NULL OR p_op IS NULL OR p_claim IS NULL OR p_attempt IS NULL
   OR p_fingerprint IS NULL OR p_fingerprint !~ '^[0-9a-f]{64}$'
   OR p_chars IS NULL OR p_chars NOT BETWEEN 1 AND 12000
   OR p_rpm IS NULL OR p_rpm NOT BETWEEN 1 AND 20
   OR p_day IS NULL OR p_day NOT BETWEEN 1 AND 1000
   OR p_parallel IS NULL OR p_parallel NOT BETWEEN 1 AND 20
   OR p_calls IS NULL OR p_calls NOT BETWEEN 1 AND 6 THEN
   RETURN QUERY SELECT 'invalid'::text,NULL::integer,NULL::integer; RETURN;
 END IF;
 actor:=public.kpdesk_provider_actor(p_org);
 IF actor IS NULL THEN RETURN QUERY SELECT 'forbidden'::text,NULL::integer,NULL::integer; RETURN; END IF;
 SELECT * INTO cfg FROM public.provider_budget_config WHERE singleton FOR UPDATE;
 IF NOT FOUND THEN
   RETURN QUERY SELECT 'ledger_unavailable'::text,NULL::integer,NULL::integer; RETURN;
 END IF;
 IF public.kpdesk_provider_actor(p_org) IS DISTINCT FROM actor THEN
   RETURN QUERY SELECT 'forbidden'::text,NULL::integer,NULL::integer; RETURN;
 END IF;
 UPDATE public.provider_budget_config SET rpm=least(rpm,p_rpm),daily=least(daily,p_day),
   parallel=least(parallel,p_parallel),calls=least(calls,p_calls) WHERE singleton
   RETURNING * INTO cfg;
 now_at:=clock_timestamp();
 UPDATE public.provider_budget_operations o SET status='ambiguous'
 WHERE status='running' AND EXISTS (SELECT 1 FROM public.provider_budget_attempts a
   WHERE a.organization_id=o.organization_id AND a.user_id=o.user_id
   AND a.operation_id=o.operation_id AND a.status='reserved' AND a.lease_expires_at<=now_at);
 UPDATE public.provider_budget_attempts SET status='ambiguous',finished_at=now_at
 WHERE status='reserved' AND lease_expires_at<=now_at;
 SELECT * INTO op FROM public.provider_budget_operations o
 WHERE o.organization_id=p_org AND o.user_id=actor AND o.operation_id=p_op;
 IF FOUND THEN
   IF op.fingerprint<>p_fingerprint THEN RETURN QUERY SELECT 'fingerprint_conflict'::text,NULL::integer,NULL::integer; RETURN; END IF;
   IF op.claim<>p_claim OR op.status<>'running' THEN RETURN QUERY SELECT 'operation_blocked'::text,NULL::integer,NULL::integer; RETURN; END IF;
 END IF;
 SELECT count(*) INTO n FROM public.provider_budget_attempts a
 WHERE a.organization_id=p_org AND a.user_id=actor AND a.operation_id=p_op;
 IF n>=cfg.calls THEN RETURN QUERY SELECT 'operation_quota'::text,NULL::integer,NULL::integer; RETURN; END IF;
 IF EXISTS (SELECT 1 FROM public.provider_budget_attempts a WHERE a.organization_id=p_org
   AND a.user_id=actor AND a.operation_id=p_op AND a.status='rejected') THEN
   RETURN QUERY SELECT 'operation_blocked'::text,NULL::integer,NULL::integer; RETURN; END IF;
 IF EXISTS (SELECT 1 FROM public.provider_budget_attempts a WHERE a.organization_id=p_org
   AND a.user_id=actor AND a.operation_id=p_op AND a.status='reserved') THEN
   RETURN QUERY SELECT 'operation_busy'::text,NULL::integer,NULL::integer; RETURN; END IF;
 IF (SELECT count(*) FROM public.provider_budget_attempts WHERE created_at>now_at-interval '1 minute')>=cfg.rpm THEN
   RETURN QUERY SELECT 'minute_quota'::text,NULL::integer,60; RETURN; END IF;
 IF (SELECT count(*) FROM public.provider_budget_attempts
   WHERE created_at >= date_trunc('day',now_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')>=cfg.daily THEN
   RETURN QUERY SELECT 'day_quota'::text,NULL::integer,
     greatest(1,ceil(extract(epoch FROM ((date_trunc('day',now_at AT TIME ZONE 'UTC')+interval '1 day') AT TIME ZONE 'UTC'-now_at)))::integer); RETURN; END IF;
 IF (SELECT count(*) FROM public.provider_budget_attempts WHERE
   status IN ('reserved','ambiguous','cancelled') AND lease_expires_at>now_at)>=cfg.parallel THEN
   RETURN QUERY SELECT 'parallel_quota'::text,NULL::integer,1; RETURN; END IF;
 INSERT INTO public.provider_budget_operations(organization_id,user_id,operation_id,fingerprint,claim,status)
 VALUES(p_org,actor,p_op,p_fingerprint,p_claim,'running') ON CONFLICT DO NOTHING;
 INSERT INTO public.provider_budget_attempts(id,organization_id,user_id,operation_id,ordinal,input_chars,status,created_at,lease_expires_at)
 VALUES(p_attempt,p_org,actor,p_op,n+1,p_chars,'reserved',now_at,now_at+interval '120 seconds');
 RETURN QUERY SELECT 'reserved'::text,n+1,NULL::integer;
END $$"""

_FINISH = """CREATE FUNCTION public.kpdesk_provider_finish(
 p_org uuid,p_op uuid,p_claim uuid,p_attempt uuid,p_outcome text,p_http integer,
 p_elapsed integer,p_prompt integer,p_completion integer,p_total integer) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE actor uuid; expiry timestamptz; expired boolean;
BEGIN
 IF p_outcome IS NULL OR p_outcome NOT IN ('success','rejected','retryable','invalid_output','ambiguous','cancelled')
 OR p_elapsed IS NULL OR p_elapsed<0 OR (p_http IS NOT NULL AND p_http NOT BETWEEN 100 AND 599)
 OR (p_outcome='retryable' AND (p_http IS NULL OR (p_http<>429 AND p_http NOT BETWEEN 500 AND 599)))
 OR p_prompt<0 OR p_completion<0 OR p_total<0 THEN RETURN 'invalid'; END IF;
 actor:=public.kpdesk_provider_actor(p_org); IF actor IS NULL THEN RETURN 'forbidden'; END IF;
 PERFORM 1 FROM public.provider_budget_config WHERE singleton FOR UPDATE;
 IF NOT FOUND THEN RETURN 'ledger_unavailable'; END IF;
 IF public.kpdesk_provider_actor(p_org) IS DISTINCT FROM actor THEN RETURN 'forbidden'; END IF;
 SELECT a.lease_expires_at INTO expiry FROM public.provider_budget_attempts a
 JOIN public.provider_budget_operations o USING(organization_id,user_id,operation_id)
 WHERE a.id=p_attempt AND a.organization_id=p_org AND a.user_id=actor AND a.operation_id=p_op
 AND a.status='reserved' AND o.status='running' AND o.claim=p_claim;
 IF NOT FOUND THEN RETURN 'operation_blocked'; END IF;
 expired:=expiry<=clock_timestamp();
 IF expired THEN p_outcome:='ambiguous'; END IF;
 UPDATE public.provider_budget_attempts SET status=p_outcome,finished_at=clock_timestamp(),
 http_status=p_http,elapsed_ms=p_elapsed,prompt_tokens=p_prompt,completion_tokens=p_completion,total_tokens=p_total
 WHERE id=p_attempt;
 IF p_outcome IN ('ambiguous','cancelled') THEN
 UPDATE public.provider_budget_operations SET status='ambiguous'
 WHERE organization_id=p_org AND user_id=actor AND operation_id=p_op; END IF;
 IF expired THEN RETURN 'ambiguous'; END IF;
 RETURN 'finished';
END $$"""

_CLOSE = """CREATE FUNCTION public.kpdesk_provider_close(
 p_org uuid,p_op uuid,p_claim uuid,p_success boolean,p_ambiguous boolean) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE actor uuid; state text;
BEGIN
 IF p_success IS NULL OR p_ambiguous IS NULL THEN RETURN 'invalid'; END IF;
 actor:=public.kpdesk_provider_actor(p_org); IF actor IS NULL THEN RETURN 'forbidden'; END IF;
 PERFORM 1 FROM public.provider_budget_config WHERE singleton FOR UPDATE;
 IF NOT FOUND THEN RETURN 'ledger_unavailable'; END IF;
 IF public.kpdesk_provider_actor(p_org) IS DISTINCT FROM actor THEN RETURN 'forbidden'; END IF;
 SELECT status INTO state FROM public.provider_budget_operations
 WHERE organization_id=p_org AND user_id=actor AND operation_id=p_op AND claim=p_claim;
 IF NOT FOUND THEN RETURN 'operation_blocked'; END IF;
 IF state='ambiguous' THEN RETURN 'closed'; END IF;
 IF state<>'running' THEN RETURN 'operation_blocked'; END IF;
 IF EXISTS(SELECT 1 FROM public.provider_budget_attempts WHERE organization_id=p_org
 AND user_id=actor AND operation_id=p_op AND status='reserved') THEN p_ambiguous:=true; END IF;
 IF p_success AND NOT p_ambiguous AND NOT EXISTS(SELECT 1 FROM public.provider_budget_attempts
 WHERE organization_id=p_org AND user_id=actor AND operation_id=p_op AND status='success')
 THEN RETURN 'invalid'; END IF;
 UPDATE public.provider_budget_attempts SET status='ambiguous',finished_at=clock_timestamp()
 WHERE organization_id=p_org AND user_id=actor AND operation_id=p_op AND status='reserved';
 UPDATE public.provider_budget_operations SET status=CASE WHEN p_ambiguous THEN 'ambiguous'
 WHEN p_success THEN 'completed' ELSE 'failed' END
 WHERE organization_id=p_org AND user_id=actor AND operation_id=p_op;
 RETURN 'closed';
END $$"""


# Reviewed 0030 cache claim, with the free model and a lease covering inference.
CACHE_CLAIM = """CREATE OR REPLACE FUNCTION public.kpdesk_llm_cache_claim(
        p_org uuid,p_attempt uuid,p_hash text,p_model text,p_version text
      ) RETURNS TABLE(cache_status text, cache_payload bytea)
      LANGUAGE plpgsql SECURITY DEFINER
      SET search_path=pg_catalog,pg_temp AS $$
      DECLARE v_actor uuid;
              v_row public.tenant_llm_extraction_cache%ROWTYPE;
              v_now timestamptz;
      BEGIN
        IF p_org IS NULL OR p_attempt IS NULL
           OR p_hash IS NULL OR p_hash !~ '^[0-9a-f]{64}$'
           OR p_version <> 'kpdesk_request_v3'
           OR p_model <> 'nvidia/nemotron-3-ultra-550b-a55b:free'
           OR NOT public.kpdesk_session_tenant_allowed(p_org,'write')
        THEN RETURN QUERY SELECT 'forbidden'::text,NULL::bytea; RETURN; END IF;
        SELECT s.user_id INTO v_actor FROM public.auth_sessions s
        JOIN public.users u ON u.id=s.user_id
        WHERE s.token_sha256=current_setting('kpdesk.session_sha256',true)
          AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp()
          AND u.status='active' AND u.email_verified_at IS NOT NULL;
        IF v_actor IS NULL OR NOT EXISTS(
          SELECT 1 FROM public.tenant_llm_attempts a
          WHERE a.id=p_attempt AND a.organization_id=p_org
            AND a.user_id=v_actor AND a.status='reserved'
            AND a.model=p_model AND a.lease_expires_at>clock_timestamp()
        ) THEN
          RETURN QUERY SELECT 'forbidden'::text,NULL::bytea; RETURN;
        END IF;
        v_now:=clock_timestamp();
        -- A bounded, lock-skipping opportunistic cleanup; stale entries
        -- can also be purged by owner-operated maintenance after expiry.
        WITH stale AS (
          SELECT c.organization_id,c.request_hmac
          FROM public.tenant_llm_extraction_cache c
          WHERE c.expires_at<=v_now
          ORDER BY c.expires_at LIMIT 100
          FOR UPDATE SKIP LOCKED
        )
        DELETE FROM public.tenant_llm_extraction_cache c
        USING stale s
        WHERE c.organization_id=s.organization_id
          AND c.request_hmac=s.request_hmac;
        INSERT INTO public.tenant_llm_extraction_cache(
          organization_id,request_hmac,model,parser_version,status,
          owner_attempt_id,created_at,expires_at
        ) VALUES (
          p_org,p_hash,p_model,p_version,'pending',
          p_attempt,v_now,v_now+interval '300 seconds'
        ) ON CONFLICT DO NOTHING;
        SELECT * INTO v_row FROM public.tenant_llm_extraction_cache c
        WHERE c.organization_id=p_org AND c.request_hmac=p_hash
        FOR UPDATE;
        IF v_row.status='ready' AND v_row.expires_at>v_now THEN
          RETURN QUERY SELECT 'hit'::text,v_row.extracted_payload; RETURN;
        END IF;
        IF v_row.status='pending' AND v_row.expires_at>v_now
          AND v_row.owner_attempt_id<>p_attempt
        THEN
          RETURN QUERY SELECT 'busy'::text,NULL::bytea; RETURN;
        END IF;
        UPDATE public.tenant_llm_extraction_cache c SET
          model=p_model,parser_version=p_version,status='pending',
          owner_attempt_id=p_attempt,extracted_payload=NULL,
          created_at=v_now,expires_at=v_now+interval '300 seconds'
        WHERE c.organization_id=p_org AND c.request_hmac=p_hash;
        RETURN QUERY SELECT 'miss'::text,NULL::bytea;
      END $$;
"""
