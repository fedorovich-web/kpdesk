"""A75 incremental checkpoint recovery, disposable PostgreSQL clone only.

This is a test prototype installer, not a production migration.
A72 stores immutable bounded checkpoint deltas; this function walks the
version-locked chain and serves an exact bounded page of the latest decisions.
"""
from __future__ import annotations

from sqlalchemy import Connection, text

from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone


def install_paged_checkpoint_recovery(connection: Connection) -> None:
    if not connection.in_transaction() or not is_live_accounts_http_clone(connection):
        raise RuntimeError("A75 recovery prototype requires a marked disposable clone.")
    if connection.scalar(text("SELECT current_user")) != "kpdesk":
        raise RuntimeError("A75 recovery requires the clone schema owner.")
    if connection.scalar(text("SELECT version_num FROM alembic_version")) != (
        "0030_tenant_llm_cache"
    ):
        raise RuntimeError("A75 recovery expects the verified schema.")
    if connection.scalar(text("""
        SELECT to_regprocedure(
            'public.a72_checkpoint_put(uuid,text,text,text,text,text,text,uuid,jsonb)'
        ) IS NOT NULL
    """)) is not True:
        raise RuntimeError("A75 recovery requires existing A72 clone tables/functions.")

    _install_paged_checkpoint_objects(connection)


def _install_paged_checkpoint_objects(connection: Connection) -> None:
    """Shared DDL, invoked only after migration or clone-owner preflight."""
    connection.execute(text("""
        CREATE FUNCTION public.a75_checkpoint_page(
          p_org uuid, p_supplier text, p_source text, p_mapping text,
          p_taxonomy text, p_price_basis text, p_offset integer, p_limit integer
        ) RETURNS TABLE (
          checkpoint_id uuid, reviewer_user_id uuid, previous_id uuid,
          taxonomy_version text, price_basis text,
          total_count integer, decisions jsonb
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
             OR p_offset IS NULL OR p_offset NOT BETWEEN 0 AND 5000
             OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 50
             OR current_setting('kpdesk.session_sha256',true) !~ '^[0-9a-f]{64}$'
             OR NOT public.kpdesk_session_tenant_allowed(p_org,'read')
          THEN RETURN; END IF;

          RETURN QUERY
          WITH RECURSIVE
          head AS MATERIALIZED (
            SELECT c.checkpoint_id,c.reviewer_user_id,c.previous_id,
                   c.taxonomy_version,c.price_basis
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
            FROM chain
            JOIN public.a72_canonical_checkpoints c
              ON c.checkpoint_id=chain.previous_id
             AND c.organization_id=p_org AND c.supplier_id=p_supplier
             AND c.source_sha256=p_source AND c.mapping_sha256=p_mapping
             AND c.taxonomy_version=p_taxonomy AND c.price_basis=p_price_basis
            WHERE chain.depth < 199
          ),
          latest_by_line AS MATERIALIZED (
            SELECT DISTINCT ON (d.source_line)
                   d.source_line,d.decision
            FROM chain
            JOIN public.a72_canonical_row_decisions d
              ON d.checkpoint_id=chain.checkpoint_id
             AND d.organization_id=p_org
            ORDER BY d.source_line,chain.depth
          ),
          counted AS MATERIALIZED (
            SELECT count(*)::integer AS n FROM latest_by_line
          ),
          selected AS MATERIALIZED (
            SELECT l.source_line,l.decision
            FROM latest_by_line l
            ORDER BY l.source_line
            LIMIT p_limit OFFSET p_offset
          )
          SELECT h.checkpoint_id,h.reviewer_user_id,h.previous_id,
                 h.taxonomy_version,h.price_basis,cnt.n,
                 coalesce((
                   SELECT jsonb_agg(s.decision ORDER BY s.source_line)
                   FROM selected s
                 ),'[]'::jsonb)
          FROM head h CROSS JOIN counted cnt
          WHERE cnt.n <= 5000
            AND NOT EXISTS (
              SELECT 1 FROM chain AS terminal
              WHERE terminal.depth=199 AND terminal.previous_id IS NOT NULL
            )
            AND (SELECT count(*) FROM chain)
                = 1 + coalesce((SELECT max(depth) FROM chain),0);
        END $$;
    """))
    connection.execute(text("""
        REVOKE ALL ON FUNCTION public.a75_checkpoint_page(
          uuid,text,text,text,text,text,integer,integer
        ) FROM PUBLIC
    """))
    connection.execute(text("""
        GRANT EXECUTE ON FUNCTION public.a75_checkpoint_page(
          uuid,text,text,text,text,text,integer,integer
        ) TO kpdesk_tenant_runtime
    """))
