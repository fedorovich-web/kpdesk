"""Shared read-only reviewed-source SQL installed by guarded Alembic revisions.

The legacy clone rehearsal uses the *identical* DDL for positive integration
proofs. No writer GRANT, signer provisioning or production activation here.
"""
from __future__ import annotations

from sqlalchemy import Connection, text

from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone

BRIDGE_SIGNATURE = "public.kpdesk_reviewed_catalog_bridge(uuid,uuid,text,text,text,text,text)"
DISCOVERY_SIGNATURE = "public.kpdesk_reviewed_supplier_versions(uuid,text,integer,integer)"
DISCOVERY_INDEX = "ix_a79_provenance_supplier_created"

BRIDGE_DDL = """
CREATE FUNCTION public.kpdesk_reviewed_catalog_bridge(
  p_org uuid,p_version uuid,p_supplier text,
  p_original_sha256 text,p_canonical_sha256 text,
  p_mapping_reject text,p_mapping_quarantine text
) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp
AS $$
  SELECT
    coalesce(current_setting('kpdesk.session_sha256',true),'')
      ~ '^[0-9a-f]{64}$'
    AND public.kpdesk_session_tenant_allowed(p_org,'read')
    AND EXISTS (
      SELECT 1 FROM public.a79_catalog_review_provenance p
      JOIN public.catalog_versions v
        ON v.organization_id=p.organization_id
       AND v.id=p.catalog_version_id
      WHERE p.organization_id=p_org
        AND p.catalog_version_id=p_version
        AND p.supplier_id=p_supplier
        AND p.original_source_sha256=p_original_sha256
        AND p.canonical_csv_sha256=p_canonical_sha256
        AND p.mapping_sha256 IN (p_mapping_reject,p_mapping_quarantine)
        AND p.price_basis='standard'
        AND v.source_sha256=p.canonical_csv_sha256
    )
$$
"""

DISCOVERY_DDL = """
CREATE FUNCTION public.kpdesk_reviewed_supplier_versions(
  p_org uuid,p_supplier text,p_limit integer,p_offset integer
) RETURNS TABLE(
  catalog_version_id uuid,original_source_sha256 text,
  mapping_sha256 text,canonical_sha256 text,rows_count integer
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp
AS $$
  SELECT p.catalog_version_id,p.original_source_sha256,
         p.mapping_sha256,p.canonical_csv_sha256,v.rows_count
  FROM public.a79_catalog_review_provenance p
  JOIN public.catalog_versions v
    ON v.organization_id=p.organization_id
   AND v.id=p.catalog_version_id
  WHERE p.organization_id=p_org
    AND p.supplier_id=p_supplier
    AND p.price_basis='standard'
    AND v.source_sha256=p.canonical_csv_sha256
    AND p_supplier ~ '^[A-Za-z0-9_.-]{1,120}$'
    AND p_limit BETWEEN 1 AND 100
    AND p_offset BETWEEN 0 AND 10000
    AND coalesce(current_setting('kpdesk.session_sha256',true),'')
        ~ '^[0-9a-f]{64}$'
    AND public.kpdesk_session_tenant_allowed(p_org,'read')
  ORDER BY p.created_at DESC,p.catalog_version_id DESC
  LIMIT p_limit OFFSET p_offset
$$
"""


def install_readonly_source_readers_on_clone(connection: Connection) -> None:
    """Exact production reader SQL on an already provisioned, tagged test clone.

    Does not activate review writers or create signing credentials. Restricted
    owner access and a fixed original clone revision prevent silent rollout.
    """
    if (
        not connection.in_transaction()
        or not is_live_accounts_http_clone(connection)
        or connection.scalar(text("SELECT current_user")) != "kpdesk"
        or connection.scalar(text("SELECT version_num FROM public.alembic_version"))
        != "0030_tenant_llm_cache"
    ):
        raise RuntimeError("Read-only source rehearsal requires a tagged clone owner.")
    for signature in (BRIDGE_SIGNATURE, DISCOVERY_SIGNATURE):
        if connection.scalar(text("SELECT to_regprocedure(:signature)"), {
            "signature": signature,
        }) is not None:
            raise RuntimeError("Read-only source functions already exist.")
    with connection.begin_nested():
        connection.execute(text(BRIDGE_DDL))
        connection.execute(text(f"""
            REVOKE ALL ON FUNCTION {BRIDGE_SIGNATURE}
            FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime
        """))
        connection.execute(text(f"""
            GRANT EXECUTE ON FUNCTION {BRIDGE_SIGNATURE} TO kpdesk_tenant_runtime
        """))
        connection.execute(text(f"""
            CREATE INDEX {DISCOVERY_INDEX}
            ON public.a79_catalog_review_provenance
            (organization_id,supplier_id,created_at DESC,catalog_version_id DESC)
        """))
        connection.execute(text(DISCOVERY_DDL))
        connection.execute(text(f"""
            REVOKE ALL ON FUNCTION {DISCOVERY_SIGNATURE}
            FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime
        """))
        connection.execute(text(f"""
            GRANT EXECUTE ON FUNCTION {DISCOVERY_SIGNATURE} TO kpdesk_tenant_runtime
        """))
