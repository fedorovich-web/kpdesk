"""Frozen additive 0038 profile contract; no changes to the 0037 DDL."""
from sqlalchemy import Connection, text

from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone

SAVE = """
CREATE OR REPLACE FUNCTION public.kpdesk_company_profile_save(p_org uuid,p_data jsonb,p_expected integer)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $$
DECLARE actor uuid; result jsonb; party jsonb; tax_id text;
        weights integer[]; total integer; i integer;
BEGIN
 party := p_data->'issuer_details';
 IF party IS NOT NULL THEN
   IF jsonb_typeof(party) IS DISTINCT FROM 'object'
   OR (party - ARRAY['full_name','short_name','inn','kpp','ogrn','address','status']) <> '{}'::jsonb
   OR EXISTS(SELECT 1 FROM unnest(ARRAY['full_name','short_name','inn']) AS x(k)
             WHERE jsonb_typeof(party->x.k) IS DISTINCT FROM 'string')
   OR length(btrim(party->>'full_name')) NOT BETWEEN 1 AND 500
   OR length(btrim(party->>'short_name')) NOT BETWEEN 1 AND 200
   OR coalesce(party->>'inn','') !~ '^([0-9]{10}|[0-9]{12})$'
   OR party->>'inn' ~ '^0+$'
   OR EXISTS(SELECT 1 FROM unnest(ARRAY['kpp','ogrn','address','status']) AS x(k)
             WHERE party ? x.k AND jsonb_typeof(party->x.k) NOT IN ('string','null'))
   OR (party->>'kpp' IS NOT NULL AND party->>'kpp' !~ '^[0-9]{9}$')
   OR (party->>'ogrn' IS NOT NULL AND party->>'ogrn' !~ '^([0-9]{13}|[0-9]{15})$')
   OR length(party->>'address')>500 OR length(party->>'status')>80
   THEN RETURN NULL; END IF;
   tax_id := party->>'inn';
   IF length(tax_id)=10 THEN
     weights := ARRAY[2,4,10,3,5,9,4,6,8];
   ELSE
     weights := ARRAY[7,2,4,10,3,5,9,4,6,8];
   END IF;
   total := 0;
   FOR i IN 1..array_length(weights,1) LOOP
     total := total + substr(tax_id,i,1)::integer * weights[i];
   END LOOP;
   IF total % 11 % 10 <> substr(tax_id,array_length(weights,1)+1,1)::integer THEN RETURN NULL; END IF;
   IF length(tax_id)=12 THEN
     weights := ARRAY[3,7,2,4,10,3,5,9,4,6,8]; total := 0;
     FOR i IN 1..11 LOOP
       total := total + substr(tax_id,i,1)::integer * weights[i];
     END LOOP;
     IF total % 11 % 10 <> substr(tax_id,12,1)::integer THEN RETURN NULL; END IF;
   END IF;
 END IF;
 IF p_org IS NULL OR p_data IS NULL OR jsonb_typeof(p_data) IS DISTINCT FROM 'object'
 OR p_expected IS NULL OR p_expected NOT BETWEEN 0 AND 2147483646
 OR octet_length(p_data::text)>12000
 OR p_data->'schema_version' IS DISTINCT FROM '1'::jsonb
 OR (p_data - ARRAY['schema_version','display_name','about','advantages','contacts','default_terms','generation_style','issuer_details']) <> '{}'::jsonb
 OR EXISTS(SELECT 1 FROM unnest(ARRAY['display_name','about','advantages','contacts','default_terms','generation_style']) AS x(k)
           WHERE jsonb_typeof(p_data->x.k) IS DISTINCT FROM 'string')
 OR length(btrim(p_data->>'display_name')) NOT BETWEEN 1 AND 200
 OR length(p_data->>'about')>1000 OR length(p_data->>'advantages')>1000
 OR length(p_data->>'contacts')>600 OR length(p_data->>'default_terms')>1000
 OR length(p_data->>'generation_style')>500
 OR coalesce(current_setting('kpdesk.session_sha256',true),'') !~ '^[0-9a-f]{64}$'
 OR NOT public.kpdesk_session_tenant_allowed(p_org,'manage') THEN RETURN NULL; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('kpdesk:company-profile:' || p_org::text,0));
 IF NOT public.kpdesk_session_tenant_allowed(p_org,'manage') THEN RETURN NULL; END IF;
 SELECT s.user_id INTO actor FROM public.auth_sessions s
 WHERE s.token_sha256=current_setting('kpdesk.session_sha256',true)
 AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp();
 IF actor IS NULL THEN RETURN NULL; END IF;
 IF p_expected=0 THEN
   INSERT INTO public.organization_proposal_profiles(organization_id,profile,updated_by)
   VALUES(p_org,p_data,actor) ON CONFLICT DO NOTHING;
 ELSE
   UPDATE public.organization_proposal_profiles SET profile=p_data,revision=revision+1,
     updated_by=actor,updated_at=clock_timestamp()
   WHERE organization_id=p_org AND revision=p_expected;
 END IF;
 IF NOT FOUND THEN RETURN NULL; END IF;
 SELECT jsonb_build_object('revision',p.revision,'data',p.profile) INTO result
 FROM public.organization_proposal_profiles p WHERE p.organization_id=p_org;
 RETURN result;
END $$
"""


def install_issuer_requisites_on_clone(connection: Connection) -> None:
    if (not connection.in_transaction() or not is_live_accounts_http_clone(connection)
        or connection.scalar(text("SELECT current_user")) != "kpdesk"):
        raise RuntimeError("Sender requisites installer requires tagged owner clone")
    connection.execute(text(SAVE))
