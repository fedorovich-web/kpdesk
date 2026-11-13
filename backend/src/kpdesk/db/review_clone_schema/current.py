"""A77 transactionally maintained current-state index for immutable audit history.

Disposable PostgreSQL clone only; shared with clone-only Alembic rehearsal. All SQL here is
installed under the existing tagged-clone, role and revision checks.
"""
from __future__ import annotations

from sqlalchemy import Connection, text

from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone


def install_current_review_index(connection: Connection) -> None:
    if not connection.in_transaction() or not is_live_accounts_http_clone(connection):
        raise RuntimeError("A77 installation requires a marked disposable clone.")
    if connection.scalar(text("SELECT current_user")) != "kpdesk":
        raise RuntimeError("A77 requires the restricted clone schema owner.")
    if connection.scalar(text("SELECT version_num FROM alembic_version")) != (
        "0030_tenant_llm_cache"
    ):
        raise RuntimeError("A77 requires the verified schema revision.")

    _install_current_review_objects(connection)


def _install_current_review_objects(connection: Connection) -> None:
    """Shared DDL, invoked only after migration or clone-owner preflight."""
    connection.execute(text("""
        CREATE TABLE public.a77_current_head (
          organization_id uuid NOT NULL,
          supplier_id text NOT NULL,
          source_sha256 text NOT NULL,
          mapping_sha256 text NOT NULL,
          taxonomy_version text NOT NULL,
          price_basis text NOT NULL,
          checkpoint_id uuid NOT NULL,
          reviewer_user_id uuid NOT NULL REFERENCES public.users(id),
          previous_id uuid REFERENCES public.a72_canonical_checkpoints(checkpoint_id),
          total_count integer NOT NULL DEFAULT 0 CHECK (total_count BETWEEN 0 AND 5000),
          PRIMARY KEY (
            organization_id,supplier_id,source_sha256,mapping_sha256,
            taxonomy_version,price_basis
          ),
          FOREIGN KEY(checkpoint_id,organization_id)
            REFERENCES public.a72_canonical_checkpoints(checkpoint_id,organization_id)
        )
    """))
    connection.execute(text("""
        CREATE TABLE public.a77_current_row (
          organization_id uuid NOT NULL,
          supplier_id text NOT NULL,
          source_sha256 text NOT NULL,
          mapping_sha256 text NOT NULL,
          taxonomy_version text NOT NULL,
          price_basis text NOT NULL,
          source_line integer NOT NULL CHECK (source_line BETWEEN 1 AND 1000000),
          source_row_sha256 text NOT NULL CHECK(source_row_sha256 ~ '^[0-9a-f]{64}$'),
          decision jsonb NOT NULL CHECK (jsonb_typeof(decision)='object'),
          checkpoint_id uuid NOT NULL,
          PRIMARY KEY (
            organization_id,supplier_id,source_sha256,mapping_sha256,
            taxonomy_version,price_basis,source_line
          ),
          FOREIGN KEY(checkpoint_id,organization_id)
            REFERENCES public.a72_canonical_checkpoints(checkpoint_id,organization_id),
          FOREIGN KEY(
            organization_id,supplier_id,source_sha256,mapping_sha256,
            taxonomy_version,price_basis
          ) REFERENCES public.a77_current_head(
            organization_id,supplier_id,source_sha256,mapping_sha256,
            taxonomy_version,price_basis
          )
        )
    """))
    for table in ("a77_current_head", "a77_current_row"):
        connection.execute(text(f"""
            REVOKE ALL ON public.{table}
            FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime
        """))
        connection.execute(text(f"""
            ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY
        """))
        connection.execute(text(f"""
            ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY
        """))
        connection.execute(text(f"""
            CREATE POLICY a77_schema_owner ON public.{table}
            FOR ALL TO kpdesk USING (true) WITH CHECK (true)
        """))

    # Install before backfilling; every future A72 append updates this index
    # in the SAME transaction, including when the existing A72 writer is used.
    connection.execute(text("""
        CREATE FUNCTION public.a77_apply_immutable_decisions()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        DECLARE current_checkpoint record;
        BEGIN
          FOR current_checkpoint IN
            SELECT DISTINCT c.*
            FROM inserted_rows r
            JOIN public.a72_canonical_checkpoints c
              ON c.checkpoint_id=r.checkpoint_id
          LOOP
            INSERT INTO public.a77_current_head(
              organization_id,supplier_id,source_sha256,mapping_sha256,
              taxonomy_version,price_basis,checkpoint_id,reviewer_user_id,
              previous_id,total_count
            ) VALUES (
              current_checkpoint.organization_id,current_checkpoint.supplier_id,
              current_checkpoint.source_sha256,current_checkpoint.mapping_sha256,
              current_checkpoint.taxonomy_version,current_checkpoint.price_basis,
              current_checkpoint.checkpoint_id,current_checkpoint.reviewer_user_id,
              current_checkpoint.previous_id,0
            ) ON CONFLICT (
              organization_id,supplier_id,source_sha256,mapping_sha256,
              taxonomy_version,price_basis
            ) DO UPDATE SET
              checkpoint_id=EXCLUDED.checkpoint_id,
              reviewer_user_id=EXCLUDED.reviewer_user_id,
              previous_id=EXCLUDED.previous_id;

            INSERT INTO public.a77_current_row(
              organization_id,supplier_id,source_sha256,mapping_sha256,
              taxonomy_version,price_basis,source_line,source_row_sha256,
              decision,checkpoint_id
            )
            SELECT current_checkpoint.organization_id,current_checkpoint.supplier_id,
                   current_checkpoint.source_sha256,current_checkpoint.mapping_sha256,
                   current_checkpoint.taxonomy_version,current_checkpoint.price_basis,
                   r.source_line,r.source_row_sha256,r.decision,r.checkpoint_id
            FROM inserted_rows r
            WHERE r.checkpoint_id=current_checkpoint.checkpoint_id
            ON CONFLICT (
              organization_id,supplier_id,source_sha256,mapping_sha256,
              taxonomy_version,price_basis,source_line
            ) DO UPDATE SET
              source_row_sha256=EXCLUDED.source_row_sha256,
              decision=EXCLUDED.decision,checkpoint_id=EXCLUDED.checkpoint_id;

            UPDATE public.a77_current_head h SET total_count=(
              SELECT count(*) FROM public.a77_current_row r
              WHERE r.organization_id=h.organization_id
                AND r.supplier_id=h.supplier_id
                AND r.source_sha256=h.source_sha256
                AND r.mapping_sha256=h.mapping_sha256
                AND r.taxonomy_version=h.taxonomy_version
                AND r.price_basis=h.price_basis
            )
            WHERE h.organization_id=current_checkpoint.organization_id
              AND h.supplier_id=current_checkpoint.supplier_id
              AND h.source_sha256=current_checkpoint.source_sha256
              AND h.mapping_sha256=current_checkpoint.mapping_sha256
              AND h.taxonomy_version=current_checkpoint.taxonomy_version
              AND h.price_basis=current_checkpoint.price_basis;
          END LOOP;
          RETURN NULL;
        END $$;
    """))
    connection.execute(text("""
        CREATE TRIGGER a77_materialize_decisions
        AFTER INSERT ON public.a72_canonical_row_decisions
        REFERENCING NEW TABLE AS inserted_rows
        FOR EACH STATEMENT
        EXECUTE FUNCTION public.a77_apply_immutable_decisions()
    """))
    connection.execute(text("""
        REVOKE ALL ON FUNCTION public.a77_apply_immutable_decisions()
        FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime
    """))

    # Backfill historical data already present in this clone (A72/A73 tests).
    # All old revisions remain immutable, and no decisions are invented.
    connection.execute(text("""
        INSERT INTO public.a77_current_head (
          organization_id,supplier_id,source_sha256,mapping_sha256,
          taxonomy_version,price_basis,checkpoint_id,reviewer_user_id,
          previous_id,total_count
        )
        SELECT DISTINCT ON (
            organization_id,supplier_id,source_sha256,mapping_sha256,
            taxonomy_version,price_basis
        )
          organization_id,supplier_id,source_sha256,mapping_sha256,
          taxonomy_version,price_basis,checkpoint_id,reviewer_user_id,
          previous_id,0
        FROM public.a72_canonical_checkpoints
        ORDER BY organization_id,supplier_id,source_sha256,mapping_sha256,
                 taxonomy_version,price_basis,created_at DESC,checkpoint_id DESC
    """))
    connection.execute(text("""
        INSERT INTO public.a77_current_row (
          organization_id,supplier_id,source_sha256,mapping_sha256,
          taxonomy_version,price_basis,source_line,source_row_sha256,
          decision,checkpoint_id
        )
        SELECT DISTINCT ON (
          c.organization_id,c.supplier_id,c.source_sha256,c.mapping_sha256,
          c.taxonomy_version,c.price_basis,d.source_line
        )
          c.organization_id,c.supplier_id,c.source_sha256,c.mapping_sha256,
          c.taxonomy_version,c.price_basis,d.source_line,d.source_row_sha256,
          d.decision,d.checkpoint_id
        FROM public.a72_canonical_row_decisions d
        JOIN public.a72_canonical_checkpoints c ON c.checkpoint_id=d.checkpoint_id
        ORDER BY c.organization_id,c.supplier_id,c.source_sha256,c.mapping_sha256,
                 c.taxonomy_version,c.price_basis,d.source_line,
                 c.created_at DESC,c.checkpoint_id DESC
    """))
    connection.execute(text("""
        UPDATE public.a77_current_head h SET total_count=(
          SELECT count(*) FROM public.a77_current_row r
          WHERE r.organization_id=h.organization_id
            AND r.supplier_id=h.supplier_id
            AND r.source_sha256=h.source_sha256
            AND r.mapping_sha256=h.mapping_sha256
            AND r.taxonomy_version=h.taxonomy_version
            AND r.price_basis=h.price_basis
        )
    """))

    connection.execute(text("""
        CREATE FUNCTION public.a77_checkpoint_page(
          p_org uuid,p_supplier text,p_source text,p_mapping text,
          p_taxonomy text,p_price_basis text,p_offset integer,p_limit integer
        ) RETURNS TABLE (
          checkpoint_id uuid,reviewer_user_id uuid,previous_id uuid,
          taxonomy_version text,price_basis text,total_count integer,decisions jsonb
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
          SELECT h.checkpoint_id,h.reviewer_user_id,h.previous_id,
                 h.taxonomy_version,h.price_basis,h.total_count,
                 COALESCE((
                   SELECT jsonb_agg(p.decision ORDER BY p.source_line)
                   FROM (
                     SELECT r.source_line,r.decision
                     FROM public.a77_current_row r
                     WHERE r.organization_id=p_org AND r.supplier_id=p_supplier
                       AND r.source_sha256=p_source AND r.mapping_sha256=p_mapping
                       AND r.taxonomy_version=p_taxonomy
                       AND r.price_basis=p_price_basis
                     ORDER BY r.source_line LIMIT p_limit OFFSET p_offset
                   ) p
                 ),'[]'::jsonb)
          FROM public.a77_current_head h
          WHERE h.organization_id=p_org AND h.supplier_id=p_supplier
            AND h.source_sha256=p_source AND h.mapping_sha256=p_mapping
            AND h.taxonomy_version=p_taxonomy AND h.price_basis=p_price_basis;
        END $$;
    """))
    connection.execute(text("""
        CREATE FUNCTION public.a77_checkpoint_probe(
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
               SELECT 1 FROM unnest(p_lines) AS item
               WHERE item IS NULL OR item NOT BETWEEN 1 AND 1000000
             )
             OR current_setting('kpdesk.session_sha256',true) !~ '^[0-9a-f]{64}$'
             OR NOT public.kpdesk_session_tenant_allowed(p_org,'read')
          THEN RETURN; END IF;
          RETURN QUERY
          SELECT h.checkpoint_id,h.total_count,
                 (SELECT count(*)::integer FROM public.a77_current_row r
                  WHERE r.organization_id=p_org AND r.supplier_id=p_supplier
                    AND r.source_sha256=p_source AND r.mapping_sha256=p_mapping
                    AND r.taxonomy_version=p_taxonomy AND r.price_basis=p_price_basis
                    AND r.source_line=ANY(p_lines))
          FROM public.a77_current_head h
          WHERE h.organization_id=p_org AND h.supplier_id=p_supplier
            AND h.source_sha256=p_source AND h.mapping_sha256=p_mapping
            AND h.taxonomy_version=p_taxonomy AND h.price_basis=p_price_basis;
        END $$;
    """))
    for signature in (
        "a77_checkpoint_page(uuid,text,text,text,text,text,integer,integer)",
        "a77_checkpoint_probe(uuid,text,text,text,text,text,integer[])",
    ):
        connection.execute(text(f"""
            REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC
        """))
        connection.execute(text(f"""
            GRANT EXECUTE ON FUNCTION public.{signature} TO kpdesk_tenant_runtime
        """))
