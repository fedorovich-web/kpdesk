"""0043 least-privilege share gateway; never installed by application startup."""
from sqlalchemy import Connection, text

SIGNATURES = (
    'public.kpdesk_proposal_share_issue(uuid,uuid,integer,text,text,bytea,text,integer)',
    'public.kpdesk_proposal_share_list(uuid,uuid)',
    'public.kpdesk_proposal_share_revoke(uuid,uuid,uuid)',
    'public.kpdesk_proposal_share_pdf(text)',
)

CREATE = """
CREATE TABLE public.proposal_shares (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 organization_id uuid NOT NULL REFERENCES public.organizations(id) ON DELETE RESTRICT,
 quote_id uuid NOT NULL,
 revision integer NOT NULL CHECK(revision>0),
 snapshot_sha256 text NOT NULL CHECK(snapshot_sha256 ~ '^[0-9a-f]{64}$'),
 token_sha256 text UNIQUE NOT NULL CHECK(token_sha256 ~ '^[0-9a-f]{64}$'),
 pdf bytea NOT NULL CHECK(octet_length(pdf) BETWEEN 5 AND 10485760),
 pdf_sha256 text NOT NULL CHECK(pdf_sha256 ~ '^[0-9a-f]{64}$'),
 created_by uuid NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 expires_at timestamptz NOT NULL,
 revoked_at timestamptz,
 CHECK(expires_at>created_at),
 FOREIGN KEY(organization_id,quote_id,revision)
   REFERENCES public.quote_revisions(organization_id,quote_id,number) ON DELETE RESTRICT
);
CREATE INDEX proposal_shares_org_quote ON public.proposal_shares(organization_id,quote_id,created_at DESC);
REVOKE ALL ON public.proposal_shares FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime;
ALTER TABLE public.proposal_shares ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.proposal_shares FORCE ROW LEVEL SECURITY;
CREATE POLICY proposal_shares_owner ON public.proposal_shares TO kpdesk USING(true) WITH CHECK(true);

CREATE FUNCTION public.kpdesk_proposal_share_issue(
 p_org uuid,p_quote uuid,p_revision integer,p_snapshot text,p_digest text,
 p_pdf bytea,p_pdf_sha text,p_days integer
) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $$
DECLARE actor uuid; result jsonb;
BEGIN
 IF p_org IS NULL OR p_quote IS NULL OR p_revision IS NULL OR p_revision<1
 OR p_snapshot IS NULL OR p_snapshot !~ '^[0-9a-f]{64}$'
 OR p_digest IS NULL OR p_digest !~ '^[0-9a-f]{64}$'
 OR p_pdf_sha IS NULL OR p_pdf_sha !~ '^[0-9a-f]{64}$'
 OR p_pdf IS NULL OR octet_length(p_pdf) NOT BETWEEN 5 AND 10485760
 OR substring(p_pdf FROM 1 FOR 5)<>decode('255044462d','hex')
 OR p_days IS NULL OR p_days NOT IN (1,7,30)
 OR NOT public.kpdesk_session_tenant_allowed(p_org,'write') THEN RETURN NULL; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('kpdesk:proposal-share:'||p_org::text,0));
 PERFORM 1 FROM public.quote_documents WHERE organization_id=p_org AND id=p_quote
   AND current_revision=p_revision FOR UPDATE;
 IF NOT FOUND OR NOT public.kpdesk_session_tenant_allowed(p_org,'write') THEN RETURN NULL; END IF;
 IF NOT EXISTS(SELECT 1 FROM public.quote_revisions WHERE organization_id=p_org
   AND quote_id=p_quote AND number=p_revision AND snapshot_sha256=p_snapshot
   AND snapshot->'approved'='true'::jsonb AND jsonb_typeof(snapshot->'document')='object'
   AND jsonb_typeof(snapshot->'approval')='object' AND is_complete AND total_rub IS NOT NULL)
 THEN RETURN NULL; END IF;
 IF (SELECT count(*) FROM public.proposal_shares WHERE organization_id=p_org)>=1000
 OR (SELECT count(*) FROM public.proposal_shares WHERE organization_id=p_org AND quote_id=p_quote
     AND revoked_at IS NULL AND expires_at>clock_timestamp())>=20 THEN RETURN NULL; END IF;
 SELECT s.user_id INTO actor FROM public.auth_sessions s
 WHERE s.token_sha256=current_setting('kpdesk.session_sha256',true)
 AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp();
 IF actor IS NULL THEN RETURN NULL; END IF;
 INSERT INTO public.proposal_shares(organization_id,quote_id,revision,snapshot_sha256,
   token_sha256,pdf,pdf_sha256,created_by,expires_at)
 VALUES(p_org,p_quote,p_revision,p_snapshot,p_digest,p_pdf,p_pdf_sha,actor,
   clock_timestamp()+make_interval(days=>p_days))
 RETURNING jsonb_build_object('id',id,'revision',revision,'created_at',created_at,
   'expires_at',expires_at,'revoked_at',revoked_at) INTO result;
 RETURN result;
END $$;

CREATE FUNCTION public.kpdesk_proposal_share_list(p_org uuid,p_quote uuid)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT coalesce(jsonb_agg(x.data ORDER BY x.created_at DESC,x.id),'[]'::jsonb) FROM (
   SELECT s.id,s.created_at,jsonb_build_object('id',s.id,'revision',s.revision,
      'created_at',s.created_at,'expires_at',s.expires_at,'revoked_at',s.revoked_at) AS data
   FROM public.proposal_shares s WHERE s.organization_id=p_org AND s.quote_id=p_quote
   AND public.kpdesk_session_tenant_allowed(p_org,'read')
 ) x
$$;

CREATE FUNCTION public.kpdesk_proposal_share_revoke(p_org uuid,p_quote uuid,p_id uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 IF NOT public.kpdesk_session_tenant_allowed(p_org,'write') THEN RETURN false; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('kpdesk:proposal-share:'||p_org::text,0));
 IF NOT public.kpdesk_session_tenant_allowed(p_org,'write') THEN RETURN false; END IF;
 IF NOT EXISTS(SELECT 1 FROM public.auth_sessions s
   WHERE s.token_sha256=current_setting('kpdesk.session_sha256',true)
   AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp()) THEN RETURN false; END IF;
 UPDATE public.proposal_shares SET revoked_at=coalesce(revoked_at,clock_timestamp())
 WHERE organization_id=p_org AND quote_id=p_quote AND id=p_id;
 RETURN FOUND;
END $$;

CREATE FUNCTION public.kpdesk_proposal_share_pdf(p_digest text)
RETURNS TABLE(pdf bytea,pdf_sha256 text) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $$
 SELECT s.pdf,s.pdf_sha256 FROM public.proposal_shares s
 WHERE p_digest ~ '^[0-9a-f]{64}$' AND s.token_sha256=p_digest
 AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp()
$$;
"""


def create_proposal_shares(connection: Connection) -> None:
    connection.execute(text(CREATE))
    for signature in SIGNATURES:
        connection.execute(text(f'REVOKE ALL ON FUNCTION {signature} FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime'))
        connection.execute(text(f'GRANT EXECUTE ON FUNCTION {signature} TO kpdesk_tenant_runtime'))
