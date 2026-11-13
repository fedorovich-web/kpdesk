"""Frozen 0037 DDL. Clone installer never targets the working database."""
from sqlalchemy import Connection, text

from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone

READ_SIGNATURE = "public.kpdesk_company_profile_read(uuid)"
SAVE_SIGNATURE = "public.kpdesk_company_profile_save(uuid,jsonb,integer)"
READ = """
CREATE FUNCTION public.kpdesk_company_profile_read(p_org uuid) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT jsonb_build_object('revision',p.revision,'data',p.profile)
 FROM public.organization_proposal_profiles p WHERE p.organization_id=p_org
 AND coalesce(current_setting('kpdesk.session_sha256',true),'') ~ '^[0-9a-f]{64}$'
 AND public.kpdesk_session_tenant_allowed(p_org,'read')
$$
"""
SAVE = """
CREATE FUNCTION public.kpdesk_company_profile_save(p_org uuid,p_data jsonb,p_expected integer)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $$
DECLARE actor uuid; result jsonb;
BEGIN
 IF p_org IS NULL OR p_data IS NULL OR jsonb_typeof(p_data) IS DISTINCT FROM 'object'
 OR p_expected IS NULL OR p_expected NOT BETWEEN 0 AND 2147483646
 OR octet_length(p_data::text)>12000
 OR p_data->'schema_version' IS DISTINCT FROM '1'::jsonb
 OR (p_data - ARRAY['schema_version','display_name','about','advantages','contacts','default_terms','generation_style']) <> '{}'::jsonb
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


def create_company_profile_objects(connection: Connection) -> None:
    statements = [
        """CREATE TABLE public.organization_proposal_profiles (
          organization_id uuid PRIMARY KEY REFERENCES public.organizations(id) ON DELETE RESTRICT,
          revision integer NOT NULL DEFAULT 1 CHECK(revision>0),
          profile jsonb NOT NULL CHECK(jsonb_typeof(profile)='object' AND octet_length(profile::text)<=12000),
          updated_by uuid NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
          updated_at timestamptz NOT NULL DEFAULT clock_timestamp())""",
        "REVOKE ALL ON public.organization_proposal_profiles FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime",
        "ALTER TABLE public.organization_proposal_profiles ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE public.organization_proposal_profiles FORCE ROW LEVEL SECURITY",
        "CREATE POLICY company_profile_owner ON public.organization_proposal_profiles TO kpdesk USING(true) WITH CHECK(true)",
        READ, SAVE,
    ]
    for statement in statements:
        connection.execute(text(statement))
    for signature in (READ_SIGNATURE, SAVE_SIGNATURE):
        connection.execute(text(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime"))
        connection.execute(text(f"GRANT EXECUTE ON FUNCTION {signature} TO kpdesk_tenant_runtime"))


def install_company_profile_on_clone(connection: Connection) -> None:
    if (not connection.in_transaction() or not is_live_accounts_http_clone(connection)
        or connection.scalar(text("SELECT current_user")) != "kpdesk"):
        raise RuntimeError("Company profile installer requires tagged owner clone")
    create_company_profile_objects(connection)
