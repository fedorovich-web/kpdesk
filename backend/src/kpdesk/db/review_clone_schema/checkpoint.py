"""A72 disposable-clone-only canonical review checkpoint proof.

Used by a guarded Alembic clone rehearsal. Not enabled in production.
The calling test must verify a provenance-tagged kpdesk_accounts_http_* clone.
"""
from __future__ import annotations

from sqlalchemy import Connection, text

from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone


def install_checkpoint_prototype(connection: Connection) -> None:
    if not connection.in_transaction() or not is_live_accounts_http_clone(connection):
        raise RuntimeError("A72 SQL prototype is restricted to a marked disposable clone.")
    if connection.scalar(text("SELECT current_user")) != "kpdesk":
        raise RuntimeError("A72 prototype requires the restricted clone schema owner.")
    if connection.scalar(text("SELECT version_num FROM alembic_version")) != (
        "0030_tenant_llm_cache"
    ):
        raise RuntimeError("A72 expects the current verified schema.")

    _install_checkpoint_objects(connection)


def _install_checkpoint_objects(connection: Connection) -> None:
    """Shared DDL, invoked only after migration or clone-owner preflight."""
    connection.execute(text("""
        CREATE TABLE public.a72_canonical_checkpoints (
          checkpoint_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          organization_id uuid NOT NULL REFERENCES public.organizations(id),
          supplier_id text NOT NULL,
          source_sha256 text NOT NULL,
          mapping_sha256 text NOT NULL,
          taxonomy_version text NOT NULL,
          price_basis text NOT NULL,
          idempotency_key text NOT NULL,
          previous_id uuid REFERENCES public.a72_canonical_checkpoints(checkpoint_id),
          reviewer_user_id uuid NOT NULL REFERENCES public.users(id),
          created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          row_count integer NOT NULL CHECK(row_count BETWEEN 1 AND 50),
          UNIQUE (checkpoint_id,organization_id),
          UNIQUE (organization_id,supplier_id,source_sha256,mapping_sha256,idempotency_key),
          CHECK(supplier_id ~ '^[A-Za-z0-9_.-]{1,120}$'),
          CHECK(source_sha256 ~ '^[0-9a-f]{64}$' AND mapping_sha256 ~ '^[0-9a-f]{64}$'),
          CHECK(length(taxonomy_version) BETWEEN 1 AND 64),
          CHECK(price_basis IN ('standard','retail','wholesale','rrp','vat_included')),
          CHECK(length(idempotency_key) BETWEEN 8 AND 128)
        )
    """))
    connection.execute(text("""
        CREATE TABLE public.a72_canonical_row_decisions (
          checkpoint_id uuid NOT NULL,
          organization_id uuid NOT NULL,
          source_line integer NOT NULL CHECK (source_line BETWEEN 1 AND 1000000),
          source_row_sha256 text NOT NULL CHECK(source_row_sha256 ~ '^[0-9a-f]{64}$'),
          decision jsonb NOT NULL CHECK(jsonb_typeof(decision)='object'),
          PRIMARY KEY(checkpoint_id,source_line),
          FOREIGN KEY(checkpoint_id,organization_id)
            REFERENCES public.a72_canonical_checkpoints(checkpoint_id,organization_id)
        )
    """))
    # The immutable checkpoint lookup and ORDER BY created_at DESC drive
    # both append and recursive recovery. Existing UNIQUE and PRIMARY KEY
    # indexes already cover idempotency and (checkpoint_id,source_line).
    connection.execute(text("""
        CREATE INDEX a72_scope_latest_idx
        ON public.a72_canonical_checkpoints (
          organization_id,supplier_id,source_sha256,mapping_sha256,
          taxonomy_version,price_basis,created_at DESC,checkpoint_id DESC
        ) INCLUDE(previous_id,reviewer_user_id)
    """))
    for table in ("a72_canonical_checkpoints", "a72_canonical_row_decisions"):
        connection.execute(text(f"""
            REVOKE ALL ON TABLE public.{table}
            FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime
        """))
        connection.execute(text(f"""
            ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY
        """))
        connection.execute(text(f"""
            ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY
        """))
        connection.execute(text(f"""
            CREATE POLICY a72_schema_owner ON public.{table}
            FOR ALL TO kpdesk USING (true) WITH CHECK (true)
        """))
        connection.execute(text(f"""
            CREATE TRIGGER a72_{table}_immutable
            BEFORE UPDATE OR DELETE ON public.{table}
            FOR EACH ROW EXECUTE FUNCTION public.kpdesk_review_immutable()
        """))
    connection.execute(text("""
        CREATE FUNCTION public.a72_checkpoint_put(
          p_org uuid,p_supplier text,p_source text,p_mapping text,
          p_taxonomy text,p_price_basis text,p_idempotency text,
          p_expected uuid,p_decisions jsonb
        ) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        DECLARE
          actor uuid;
          prior uuid;
          result uuid;
          existing record;
          n integer;
        BEGIN
          IF p_org IS NULL OR p_supplier IS NULL
             OR p_supplier !~ '^[A-Za-z0-9_.-]{1,120}$'
             OR p_source IS NULL OR p_source !~ '^[0-9a-f]{64}$'
             OR p_mapping IS NULL OR p_mapping !~ '^[0-9a-f]{64}$'
             OR p_taxonomy IS NULL OR length(p_taxonomy) NOT BETWEEN 1 AND 64
             OR p_price_basis IS NULL
             OR p_price_basis NOT IN ('standard','retail','wholesale','rrp','vat_included')
             OR p_idempotency IS NULL OR length(p_idempotency) NOT BETWEEN 8 AND 128
             OR p_decisions IS NULL OR jsonb_typeof(p_decisions)<>'array'
             OR length(p_decisions::text)>65536
             OR current_setting('kpdesk.session_sha256',true) !~ '^[0-9a-f]{64}$'
             OR NOT public.kpdesk_session_tenant_allowed(p_org,'write')
          THEN RETURN NULL; END IF;
          n := jsonb_array_length(p_decisions);
          IF n NOT BETWEEN 1 AND 50 THEN RETURN NULL; END IF;
          IF EXISTS (
            SELECT 1 FROM jsonb_array_elements(p_decisions) AS value
            WHERE jsonb_typeof(value)<>'object'
               OR value->>'line' IS NULL
               OR value->>'line' !~ '^[1-9][0-9]{0,5}$'
               OR value->>'source_row_sha256' IS NULL
               OR value->>'source_row_sha256' !~ '^[0-9a-f]{64}$'
               OR jsonb_typeof(value->'confirmed') IS DISTINCT FROM 'boolean'
               OR value->>'confirmed' IS DISTINCT FROM 'true'
               OR value->>'action' IS NULL
               OR value->>'action' NOT IN ('include','exclude')
               OR (value->>'action'='exclude'
                   AND (value->>'reason' IS NULL
                        OR length(trim(value->>'reason')) NOT BETWEEN 8 AND 500))
               OR (value->>'action'='include'
                   AND (value->>'category' IS NULL
                     OR value->>'sale_unit' IS NULL
                     OR value->>'sale_unit' NOT IN ('штука','пачка','коробка')
                     OR value->>'pieces_per_sale_unit' IS NULL
                     OR value->>'pieces_per_sale_unit' !~ '^[1-9][0-9]{0,5}$'
                     OR value->'attributes' IS NULL
                     OR jsonb_typeof(value->'attributes') <> 'object'))
          ) OR n <> (
            SELECT count(DISTINCT (value->>'line')::integer)
            FROM jsonb_array_elements(p_decisions) AS value
          ) THEN RETURN NULL; END IF;
          SELECT s.user_id INTO actor
          FROM public.auth_sessions s
          JOIN public.users u ON u.id=s.user_id
          JOIN public.organization_memberships m
            ON m.organization_id=p_org AND m.user_id=s.user_id
          WHERE s.token_sha256=current_setting('kpdesk.session_sha256',true)
            AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp()
            AND u.status='active' AND u.email_verified_at IS NOT NULL
            AND m.status='active' AND m.role IN ('owner','editor');
          IF actor IS NULL THEN RETURN NULL; END IF;
          PERFORM pg_advisory_xact_lock(hashtextextended(
            p_org::text || p_supplier || p_source || p_mapping,0
          ));
          SELECT c.checkpoint_id,c.reviewer_user_id,c.previous_id,c.row_count,
                 c.taxonomy_version,c.price_basis
            INTO existing
          FROM public.a72_canonical_checkpoints c
          WHERE c.organization_id=p_org AND c.supplier_id=p_supplier
            AND c.source_sha256=p_source AND c.mapping_sha256=p_mapping
            AND c.idempotency_key=p_idempotency;
          IF FOUND THEN
            IF existing.reviewer_user_id=actor
               AND existing.row_count=n
               AND existing.taxonomy_version=p_taxonomy
               AND existing.price_basis=p_price_basis
               AND existing.previous_id IS NOT DISTINCT FROM p_expected
               AND (SELECT jsonb_agg(d.decision ORDER BY d.source_line)
                    FROM public.a72_canonical_row_decisions d
                    WHERE d.checkpoint_id=existing.checkpoint_id)
                  = (SELECT jsonb_agg(value ORDER BY (value->>'line')::integer)
                     FROM jsonb_array_elements(p_decisions) AS value)
            THEN RETURN existing.checkpoint_id; END IF;
            RAISE EXCEPTION 'canonical checkpoint idempotency conflict'
              USING ERRCODE='23505';
          END IF;
          SELECT c.checkpoint_id INTO prior
          FROM public.a72_canonical_checkpoints c
          WHERE c.organization_id=p_org AND c.supplier_id=p_supplier
            AND c.source_sha256=p_source AND c.mapping_sha256=p_mapping
            AND c.taxonomy_version=p_taxonomy AND c.price_basis=p_price_basis
          ORDER BY c.created_at DESC,c.checkpoint_id DESC LIMIT 1;
          IF prior IS DISTINCT FROM p_expected THEN
            RAISE EXCEPTION 'stale canonical checkpoint'
              USING ERRCODE='40001';
          END IF;
          INSERT INTO public.a72_canonical_checkpoints(
            organization_id,supplier_id,source_sha256,mapping_sha256,
            taxonomy_version,price_basis,idempotency_key,previous_id,
            reviewer_user_id,row_count
          ) VALUES (
            p_org,p_supplier,p_source,p_mapping,p_taxonomy,p_price_basis,
            p_idempotency,p_expected,actor,n
          ) RETURNING checkpoint_id INTO result;
          INSERT INTO public.a72_canonical_row_decisions(
            checkpoint_id,organization_id,source_line,source_row_sha256,decision
          )
          SELECT result,p_org,(value->>'line')::integer,
                 value->>'source_row_sha256',value
          FROM jsonb_array_elements(p_decisions) AS value;
          RETURN result;
        END $$;
    """))
    connection.execute(text("""
        CREATE FUNCTION public.a72_checkpoint_get(
          p_org uuid,p_supplier text,p_source text,p_mapping text,
          p_taxonomy text,p_price_basis text
        ) RETURNS TABLE (
          checkpoint_id uuid,reviewer_user_id uuid,previous_id uuid,
          taxonomy_version text,price_basis text,decisions jsonb
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
             OR current_setting('kpdesk.session_sha256',true) !~ '^[0-9a-f]{64}$'
             OR NOT public.kpdesk_session_tenant_allowed(p_org,'read')
          THEN RETURN; END IF;
          RETURN QUERY
          SELECT c.checkpoint_id,c.reviewer_user_id,c.previous_id,
                 c.taxonomy_version,c.price_basis,
                 (SELECT jsonb_agg(d.decision ORDER BY d.source_line)
                  FROM public.a72_canonical_row_decisions d
                  WHERE d.checkpoint_id=c.checkpoint_id)
          FROM public.a72_canonical_checkpoints c
          WHERE c.organization_id=p_org AND c.supplier_id=p_supplier
            AND c.source_sha256=p_source AND c.mapping_sha256=p_mapping
            AND c.taxonomy_version=p_taxonomy AND c.price_basis=p_price_basis
          ORDER BY c.created_at DESC,c.checkpoint_id DESC LIMIT 1;
        END $$;
    """))
    for signature in (
        "a72_checkpoint_put(uuid,text,text,text,text,text,text,uuid,jsonb)",
        "a72_checkpoint_get(uuid,text,text,text,text,text)",
    ):
        connection.execute(text(f"""
            REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC
        """))
        connection.execute(text(f"""
            GRANT EXECUTE ON FUNCTION public.{signature} TO kpdesk_tenant_runtime
        """))
