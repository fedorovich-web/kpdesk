"""Frozen, bounded tenant supplier-directory SQL for Alembic and acceptance."""
from __future__ import annotations

FUNCTION_LIST = """
CREATE FUNCTION public.kpdesk_supplier_registry_list(
  p_org uuid,p_limit integer,p_offset integer
) RETURNS TABLE (
  supplier_id text,display_name text,notes text,status text,revision integer
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp
AS $$
  SELECT r.supplier_id::text,r.display_name::text,r.notes::text,
         r.status,r.revision
  FROM public.tenant_supplier_registry r
  WHERE r.organization_id=p_org
    AND p_limit BETWEEN 1 AND 100
    AND p_offset BETWEEN 0 AND 10000
    AND coalesce(current_setting('kpdesk.session_sha256',true),'')
        ~ '^[0-9a-f]{64}$'
    AND public.kpdesk_session_tenant_allowed(p_org,'read')
  ORDER BY lower(r.display_name),r.supplier_id
  LIMIT p_limit OFFSET p_offset
$$
"""

FUNCTION_UPSERT = """
CREATE FUNCTION public.kpdesk_supplier_registry_upsert(
  p_org uuid,p_supplier text,p_name text,p_notes text,p_status text,
  p_expected_revision integer
) RETURNS TABLE (
  supplier_id text,display_name text,notes text,status text,revision integer
)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp
AS $$
DECLARE
  actor uuid;
BEGIN
  IF p_org IS NULL OR p_supplier IS NULL
    OR p_supplier !~ '^[A-Za-z0-9_.-]{1,120}$'
    OR p_name IS NULL OR length(btrim(p_name)) NOT BETWEEN 1 AND 200
    OR p_notes IS NULL OR length(p_notes)>1000
    OR p_status NOT IN ('active','archived')
    OR p_expected_revision IS NULL
    OR p_expected_revision NOT BETWEEN 0 AND 2147483646
    OR coalesce(current_setting('kpdesk.session_sha256',true),'')
       !~ '^[0-9a-f]{64}$'
    OR NOT public.kpdesk_session_tenant_allowed(p_org,'write')
  THEN RETURN; END IF;
  SELECT s.user_id INTO actor
  FROM public.auth_sessions s
  JOIN public.users u ON u.id=s.user_id
  JOIN public.organization_memberships m
    ON m.user_id=s.user_id AND m.organization_id=p_org
  WHERE s.token_sha256=current_setting('kpdesk.session_sha256',true)
    AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp()
    AND u.status='active' AND u.email_verified_at IS NOT NULL
    AND m.status='active' AND m.role IN ('owner','editor');
  IF actor IS NULL THEN RETURN; END IF;
  IF p_expected_revision=0 THEN
    INSERT INTO public.tenant_supplier_registry (
      organization_id,supplier_id,display_name,notes,status,created_by,updated_by
    ) VALUES (
      p_org,p_supplier,btrim(p_name),p_notes,p_status,actor,actor
    ) ON CONFLICT DO NOTHING;
    IF NOT FOUND THEN RETURN; END IF;
  ELSE
    UPDATE public.tenant_supplier_registry r
    SET display_name=btrim(p_name),notes=p_notes,status=p_status,
        revision=r.revision+1,updated_by=actor,updated_at=clock_timestamp()
    WHERE r.organization_id=p_org AND r.supplier_id=p_supplier
      AND r.revision=p_expected_revision;
    IF NOT FOUND THEN RETURN; END IF;
  END IF;
  RETURN QUERY
  SELECT r.supplier_id::text,r.display_name::text,r.notes::text,
         r.status,r.revision
  FROM public.tenant_supplier_registry r
  WHERE r.organization_id=p_org AND r.supplier_id=p_supplier;
END $$
"""


FUNCTION_STATUS = """
CREATE FUNCTION public.kpdesk_supplier_registry_status(
  p_org uuid,p_supplier text
) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp
AS $$
  SELECT r.status
  FROM public.tenant_supplier_registry r
  WHERE r.organization_id=p_org
    AND r.supplier_id=p_supplier
    AND p_supplier ~ '^[A-Za-z0-9_.-]{1,120}$'
    AND coalesce(current_setting('kpdesk.session_sha256',true),'')
        ~ '^[0-9a-f]{64}$'
    AND public.kpdesk_session_tenant_allowed(p_org,'read')
$$
"""
