"""0040 frozen DDL; no runtime schema creation or filesystem storage fallback."""
from sqlalchemy import Connection, text

from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone

FUNCTIONS = {
    'kpdesk_training_feedback_actor(uuid)': """
CREATE FUNCTION public.kpdesk_training_feedback_actor(p_org uuid) RETURNS uuid
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT s.user_id FROM public.auth_sessions s
 WHERE s.token_sha256=current_setting('kpdesk.session_sha256',true)
 AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp()
 AND public.kpdesk_session_tenant_allowed(p_org,'manage')
$$""",
    'kpdesk_training_feedback_summary(uuid,uuid)': """
CREATE FUNCTION public.kpdesk_training_feedback_summary(p_org uuid,p_id uuid) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT jsonb_build_object('feedback_id',f.id,'quote_id',f.quote_id,'revision',f.revision,
   'reviewer_id',f.reviewer_id,'created_at',f.created_at,'withdrawn_at',w.created_at,
   'event_count',jsonb_array_length(f.bundle->'events'),'curation_status','pending_review')
 FROM public.training_feedback_records f LEFT JOIN public.training_feedback_withdrawals w
 ON w.organization_id=f.organization_id AND w.feedback_id=f.id
 WHERE f.organization_id=p_org AND f.id=p_id
$$""",
    'kpdesk_training_feedback_retry(uuid,uuid,integer,text,text,text)': """
CREATE FUNCTION public.kpdesk_training_feedback_retry(p_org uuid,p_quote uuid,p_revision integer,p_sha text,p_digest text,p_key text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE actor uuid; existing public.training_feedback_records%ROWTYPE;
BEGIN
 actor:=public.kpdesk_training_feedback_actor(p_org);
 IF actor IS NULL THEN RETURN NULL; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('kpdesk:feedback:'||p_org::text,0));
 IF public.kpdesk_training_feedback_actor(p_org) IS DISTINCT FROM actor THEN RETURN NULL; END IF;
 SELECT * INTO existing FROM public.training_feedback_records
 WHERE organization_id=p_org AND idempotency_key=p_key FOR SHARE;
 IF NOT FOUND THEN RETURN NULL; END IF;
 IF public.kpdesk_training_feedback_actor(p_org) IS DISTINCT FROM actor THEN RETURN NULL; END IF;
 IF existing.request_sha256 IS DISTINCT FROM p_digest OR existing.reviewer_id IS DISTINCT FROM actor
 OR existing.quote_id IS DISTINCT FROM p_quote OR existing.revision IS DISTINCT FROM p_revision
 OR existing.snapshot_sha256 IS DISTINCT FROM p_sha
 THEN RETURN jsonb_build_object('error','feedback_key_conflict'); END IF;
 IF EXISTS(SELECT 1 FROM public.training_feedback_withdrawals WHERE organization_id=p_org AND feedback_id=existing.id)
 THEN RETURN jsonb_build_object('error','feedback_withdrawn'); END IF;
 RETURN public.kpdesk_training_feedback_summary(p_org,existing.id);
END $$""",
    'kpdesk_training_feedback_save(uuid,jsonb,text,text)': """
CREATE FUNCTION public.kpdesk_training_feedback_save(p_org uuid,p_bundle jsonb,p_digest text,p_key text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE actor uuid; existing public.training_feedback_records%ROWTYPE; new_id uuid;
        head integer; source_sha text; source_catalog uuid;
BEGIN
 actor:=public.kpdesk_training_feedback_actor(p_org);
 IF actor IS NULL THEN RETURN NULL; END IF;
 IF p_bundle IS NULL OR jsonb_typeof(p_bundle) IS DISTINCT FROM 'object'
 OR octet_length(p_bundle::text)>10485760 OR p_digest IS NULL OR p_digest !~ '^[0-9a-f]{64}$'
 OR p_key IS NULL OR length(p_key) NOT BETWEEN 1 AND 128 OR p_key !~ '^[!-~]+$'
 OR p_bundle->>'format' IS DISTINCT FROM 'kpdesk-manager-feedback-v1'
 OR p_bundle->>'training_data_kind' IS DISTINCT FROM 'manager_reviewed'
 OR p_bundle->'training_consent' IS DISTINCT FROM 'true'::jsonb
 OR p_bundle->>'organization_id' IS DISTINCT FROM p_org::text
 OR p_bundle->>'reviewer_id' IS DISTINCT FROM actor::text
 OR jsonb_typeof(p_bundle->'events') IS DISTINCT FROM 'array'
 OR jsonb_typeof(p_bundle->'catalog') IS DISTINCT FROM 'array'
 THEN RETURN jsonb_build_object('error','feedback_invalid'); END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('kpdesk:feedback:'||p_org::text,0));
 IF public.kpdesk_training_feedback_actor(p_org) IS DISTINCT FROM actor THEN RETURN NULL; END IF;
 SELECT * INTO existing FROM public.training_feedback_records
 WHERE organization_id=p_org AND idempotency_key=p_key FOR SHARE;
 IF FOUND THEN
   IF existing.request_sha256<>p_digest OR existing.reviewer_id<>actor
   OR existing.quote_id::text IS DISTINCT FROM p_bundle->>'quote_id'
   OR existing.revision::text IS DISTINCT FROM p_bundle->>'revision'
   OR existing.snapshot_sha256 IS DISTINCT FROM p_bundle->>'snapshot_sha256'
   THEN RETURN jsonb_build_object('error','feedback_key_conflict'); END IF;
   IF EXISTS(SELECT 1 FROM public.training_feedback_withdrawals
             WHERE organization_id=p_org AND feedback_id=existing.id)
   THEN RETURN jsonb_build_object('error','feedback_withdrawn'); END IF;
   RETURN public.kpdesk_training_feedback_summary(p_org,existing.id);
 END IF;
 SELECT d.current_revision,r.snapshot_sha256,r.catalog_version_id INTO head,source_sha,source_catalog
 FROM public.quote_documents d JOIN public.quote_revisions r
 ON r.organization_id=d.organization_id AND r.quote_id=d.id AND r.number=d.current_revision
 WHERE d.organization_id=p_org AND d.id=(p_bundle->>'quote_id')::uuid FOR SHARE OF d;
 IF head IS NULL OR head::text IS DISTINCT FROM p_bundle->>'revision'
 OR source_sha IS DISTINCT FROM p_bundle->>'snapshot_sha256'
 OR source_catalog::text IS DISTINCT FROM p_bundle->>'catalog_version_id'
 THEN RETURN jsonb_build_object('error','stale_revision'); END IF;
 IF public.kpdesk_training_feedback_actor(p_org) IS DISTINCT FROM actor THEN RETURN NULL; END IF;
 IF (SELECT count(*)>=500 OR coalesce(sum(octet_length(bundle::text)),0)+octet_length(p_bundle::text)>104857600
     FROM public.training_feedback_records WHERE organization_id=p_org)
 THEN RETURN jsonb_build_object('error','feedback_storage_limit'); END IF;
 new_id:=gen_random_uuid();
 INSERT INTO public.training_feedback_records(id,organization_id,quote_id,revision,snapshot_sha256,
   reviewer_id,idempotency_key,request_sha256,bundle)
 VALUES(new_id,p_org,(p_bundle->>'quote_id')::uuid,head,source_sha,actor,p_key,p_digest,p_bundle);
 RETURN public.kpdesk_training_feedback_summary(p_org,new_id);
END $$""",
    'kpdesk_training_feedback_history(uuid,uuid,uuid)': """
CREATE FUNCTION public.kpdesk_training_feedback_history(p_org uuid,p_quote uuid,p_before uuid) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE cursor_time timestamptz; result jsonb;
BEGIN
 IF public.kpdesk_training_feedback_actor(p_org) IS NULL THEN RETURN NULL; END IF;
 IF NOT EXISTS(SELECT 1 FROM public.quote_documents WHERE organization_id=p_org AND id=p_quote) THEN RETURN NULL; END IF;
 IF p_before IS NOT NULL THEN
   SELECT created_at INTO cursor_time FROM public.training_feedback_records
   WHERE organization_id=p_org AND quote_id=p_quote AND id=p_before;
   IF NOT FOUND THEN RETURN jsonb_build_object('error','feedback_cursor_invalid'); END IF;
 END IF;
 SELECT jsonb_build_object('items',coalesce(jsonb_agg(summary ORDER BY created_at DESC,id DESC),'[]'::jsonb),
   'next_cursor',CASE WHEN count(*)=20 THEN (array_agg(id ORDER BY created_at,id))[1] ELSE NULL END)
 INTO result FROM (
   SELECT id,created_at,public.kpdesk_training_feedback_summary(p_org,id) AS summary
   FROM public.training_feedback_records WHERE organization_id=p_org AND quote_id=p_quote
   AND (p_before IS NULL OR (created_at,id)<(cursor_time,p_before))
   ORDER BY created_at DESC,id DESC LIMIT 20
 ) page;
 RETURN result;
END $$""",
    'kpdesk_training_feedback_get(uuid,uuid,uuid)': """
CREATE FUNCTION public.kpdesk_training_feedback_get(p_org uuid,p_quote uuid,p_id uuid) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE result jsonb;
BEGIN
 IF public.kpdesk_training_feedback_actor(p_org) IS NULL THEN RETURN NULL; END IF;
 SELECT bundle INTO result FROM public.training_feedback_records
 WHERE organization_id=p_org AND quote_id=p_quote AND id=p_id FOR SHARE;
 IF result IS NULL OR public.kpdesk_training_feedback_actor(p_org) IS NULL THEN RETURN NULL; END IF;
 IF EXISTS(SELECT 1 FROM public.training_feedback_withdrawals WHERE organization_id=p_org AND feedback_id=p_id)
 THEN RETURN jsonb_build_object('error','feedback_withdrawn'); END IF;
 RETURN result;
END $$""",
    'kpdesk_training_feedback_withdraw(uuid,uuid,uuid)': """
CREATE FUNCTION public.kpdesk_training_feedback_withdraw(p_org uuid,p_quote uuid,p_id uuid) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE actor uuid;
BEGIN
 actor:=public.kpdesk_training_feedback_actor(p_org);
 IF actor IS NULL THEN RETURN NULL; END IF;
 PERFORM 1 FROM public.training_feedback_records WHERE organization_id=p_org AND quote_id=p_quote AND id=p_id FOR UPDATE;
 IF NOT FOUND OR public.kpdesk_training_feedback_actor(p_org) IS DISTINCT FROM actor THEN RETURN NULL; END IF;
 INSERT INTO public.training_feedback_withdrawals(organization_id,feedback_id,withdrawn_by)
 VALUES(p_org,p_id,actor) ON CONFLICT DO NOTHING;
 RETURN public.kpdesk_training_feedback_summary(p_org,p_id);
END $$""",
}


def create_training_feedback_objects(connection: Connection) -> None:
    statements = ["""CREATE TABLE public.training_feedback_records(
      id uuid NOT NULL, organization_id uuid NOT NULL, quote_id uuid NOT NULL,
      revision integer NOT NULL CHECK(revision>0), snapshot_sha256 text NOT NULL CHECK(snapshot_sha256 ~ '^[0-9a-f]{64}$'),
      reviewer_id uuid NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      idempotency_key text NOT NULL CHECK(length(idempotency_key) BETWEEN 1 AND 128),
      request_sha256 text NOT NULL CHECK(request_sha256 ~ '^[0-9a-f]{64}$'),
      bundle jsonb NOT NULL CHECK(jsonb_typeof(bundle)='object' AND octet_length(bundle::text)<=10485760
        AND jsonb_array_length(bundle->'events') BETWEEN 1 AND 420
        AND jsonb_array_length(bundle->'catalog') BETWEEN 1 AND 5000),
      PRIMARY KEY(organization_id,id), UNIQUE(organization_id,idempotency_key),
      FOREIGN KEY(organization_id,quote_id,revision)
        REFERENCES public.quote_revisions(organization_id,quote_id,number) ON DELETE RESTRICT)""",
      "CREATE INDEX training_feedback_history_idx ON public.training_feedback_records(organization_id,quote_id,created_at DESC,id DESC)",
      """CREATE TABLE public.training_feedback_withdrawals(
        organization_id uuid NOT NULL, feedback_id uuid NOT NULL,
        withdrawn_by uuid NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(organization_id,feedback_id),
        FOREIGN KEY(organization_id,feedback_id) REFERENCES public.training_feedback_records(organization_id,id) ON DELETE RESTRICT)""",
      """CREATE FUNCTION public.kpdesk_training_feedback_immutable() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
        BEGIN RAISE EXCEPTION 'Training feedback audit is immutable'; END $$""",
    ]
    for statement in statements:
        connection.execute(text(statement))
    for table in ('training_feedback_records', 'training_feedback_withdrawals'):
        for statement in (
            f'REVOKE ALL ON public.{table} FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime',
            f'ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY',
            f'ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY',
            f'CREATE POLICY feedback_owner ON public.{table} TO kpdesk USING(true) WITH CHECK(true)',
            f'CREATE TRIGGER feedback_immutable BEFORE UPDATE OR DELETE ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.kpdesk_training_feedback_immutable()',
        ):
            connection.execute(text(statement))
    connection.execute(text('REVOKE ALL ON FUNCTION public.kpdesk_training_feedback_immutable() FROM PUBLIC'))
    for signature, statement in FUNCTIONS.items():
        connection.execute(text(statement))
        connection.execute(text(f'REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime'))
        if '_actor(' not in signature and '_summary(' not in signature:
            connection.execute(text(f'GRANT EXECUTE ON FUNCTION public.{signature} TO kpdesk_tenant_runtime'))


def install_training_feedback_on_clone(connection: Connection) -> None:
    if (not connection.in_transaction() or not is_live_accounts_http_clone(connection)
            or connection.scalar(text('SELECT current_user')) != 'kpdesk'):
        raise RuntimeError('Feedback installer requires tagged owner clone')
    create_training_feedback_objects(connection)


def drop_empty_training_feedback_objects(connection: Connection) -> None:
    if connection.scalar(text('SELECT count(*) FROM public.training_feedback_records')) != 0:
        raise RuntimeError('Feedback contains business data; refusing downgrade')
    for signature in reversed(FUNCTIONS):
        connection.execute(text(f'DROP FUNCTION public.{signature} RESTRICT'))
    for table in ('training_feedback_withdrawals', 'training_feedback_records'):
        connection.execute(text(f'DROP TABLE public.{table} RESTRICT'))
    connection.execute(text('DROP FUNCTION public.kpdesk_training_feedback_immutable() RESTRICT'))
