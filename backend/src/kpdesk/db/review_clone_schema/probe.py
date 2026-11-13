"""A76 single-pass overlap/count probe, disposable PostgreSQL clone only.

Used by clone-only Alembic rehearsal; never install outside marked clones.
"""
from __future__ import annotations

from sqlalchemy import Connection, text

from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone


def install_checkpoint_probe(connection: Connection) -> None:
    if not connection.in_transaction() or not is_live_accounts_http_clone(connection):
        raise RuntimeError("A76 probe requires a marked disposable clone transaction.")
    if connection.scalar(text("SELECT current_user")) != "kpdesk":
        raise RuntimeError("A76 probe requires clone schema owner.")
    if connection.scalar(text("SELECT version_num FROM alembic_version")) != (
        "0030_tenant_llm_cache"
    ):
        raise RuntimeError("A76 probe expects verified schema.")

    _install_checkpoint_probe_objects(connection)


def _install_checkpoint_probe_objects(connection: Connection) -> None:
    """Shared DDL, invoked only after migration or clone-owner preflight."""
    connection.execute(text("""
        CREATE FUNCTION public.a76_checkpoint_probe(
          p_org uuid,p_supplier text,p_source text,p_mapping text,
          p_taxonomy text,p_price_basis text,p_lines integer[]
        ) RETURNS TABLE(
          checkpoint_id uuid,total_count integer,overlap_count integer
        ) LANGUAGE plpgsql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        BEGIN
          IF p_org IS NULL OR p_supplier IS NULL
             OR p_supplier !~ '^[A-Za-z0-9_.-]{1,120}$'
             OR p_source IS NULL OR p_source !~ '^[0-9a-f]{64}$'
             OR p_mapping IS NULL OR p_mapping !~ '^[0-9a-f]{64}$'
             OR p_taxonomy IS NULL OR length(p_taxonomy) NOT BETWEEN 1 AND 64
             OR p_price_basis IS NULL
             OR p_price_basis NOT IN ('standard','retail','wholesale','rrp','vat_included')
             OR p_lines IS NULL OR cardinality(p_lines) NOT BETWEEN 1 AND 50
             OR EXISTS (
                SELECT 1 FROM unnest(p_lines) AS source_line
                WHERE source_line IS NULL OR source_line NOT BETWEEN 1 AND 1000000
             )
             OR current_setting('kpdesk.session_sha256',true) !~ '^[0-9a-f]{64}$'
             OR NOT public.kpdesk_session_tenant_allowed(p_org,'read')
          THEN RETURN; END IF;

          RETURN QUERY
          WITH RECURSIVE
          head AS MATERIALIZED (
            SELECT c.checkpoint_id,c.previous_id
            FROM public.a72_canonical_checkpoints c
            WHERE c.organization_id=p_org AND c.supplier_id=p_supplier
              AND c.source_sha256=p_source AND c.mapping_sha256=p_mapping
              AND c.taxonomy_version=p_taxonomy AND c.price_basis=p_price_basis
            ORDER BY c.created_at DESC,c.checkpoint_id DESC LIMIT 1
          ),
          chain AS (
            SELECT h.checkpoint_id,h.previous_id,0 AS depth FROM head h
            UNION ALL
            SELECT c.checkpoint_id,c.previous_id,chain.depth+1
            FROM chain JOIN public.a72_canonical_checkpoints c
              ON c.checkpoint_id=chain.previous_id
             AND c.organization_id=p_org AND c.supplier_id=p_supplier
             AND c.source_sha256=p_source AND c.mapping_sha256=p_mapping
             AND c.taxonomy_version=p_taxonomy AND c.price_basis=p_price_basis
            WHERE chain.depth < 199
          ),
          latest_by_line AS MATERIALIZED (
            SELECT DISTINCT ON (d.source_line) d.source_line
            FROM chain JOIN public.a72_canonical_row_decisions d
              ON d.checkpoint_id=chain.checkpoint_id AND d.organization_id=p_org
            ORDER BY d.source_line,chain.depth
          )
          SELECT h.checkpoint_id,count(l.source_line)::integer,
                 count(l.source_line) FILTER (
                   WHERE l.source_line=ANY(p_lines)
                 )::integer
          FROM head h LEFT JOIN latest_by_line l ON TRUE
          WHERE NOT EXISTS (
            SELECT 1 FROM chain AS terminal
            WHERE terminal.depth=199 AND terminal.previous_id IS NOT NULL
          )
          AND (SELECT count(*) FROM chain)
              = 1 + coalesce((SELECT max(depth) FROM chain),0)
          GROUP BY h.checkpoint_id;
        END $$;
    """))
    connection.execute(text("""
        REVOKE ALL ON FUNCTION public.a76_checkpoint_probe(
          uuid,text,text,text,text,text,integer[]
        ) FROM PUBLIC
    """))
    connection.execute(text("""
        GRANT EXECUTE ON FUNCTION public.a76_checkpoint_probe(
          uuid,text,text,text,text,text,integer[]
        ) TO kpdesk_tenant_runtime
    """))
