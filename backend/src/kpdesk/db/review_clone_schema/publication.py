"""A79 provenance schema: disposable-clone-only, not a production migration."""
from __future__ import annotations

import secrets

from sqlalchemy import Connection, text

from kpdesk.catalog.review_signing import ReviewSigningCredential
from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone


def install_publication_provenance(connection: Connection) -> ReviewSigningCredential:
    if not connection.in_transaction() or not is_live_accounts_http_clone(connection):
        raise RuntimeError("A79 schema requires an authenticated disposable clone.")
    if connection.scalar(text("SELECT current_user")) != "kpdesk":
        raise RuntimeError("A79 schema requires the clone schema owner.")
    if connection.scalar(text("SELECT version_num FROM alembic_version")) != "0030_tenant_llm_cache":
        raise RuntimeError("A79 requires verified migrations.")
    # A84: this key is generated for the disposable clone only and is supplied
    # only to the trusted source-replay service. Never expose it to DB runtime.
    secret = secrets.token_bytes(32)
    connection.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public"))
    _install_publication_objects(connection, signing_secret=secret)
    return ReviewSigningCredential(epoch=1, key=secret)


def _install_publication_objects(
    connection: Connection, *, signing_secret: bytes | None,
) -> None:
    """Shared publication DDL. A None key leaves production in inactive state."""
    connection.execute(text("""
        CREATE TABLE public.a84_provenance_signing_secret (
          key_epoch bigint PRIMARY KEY CHECK (key_epoch BETWEEN 1 AND 9223372036854775807),
          secret bytea NOT NULL CHECK (octet_length(secret)=32),
          created_at timestamptz NOT NULL DEFAULT clock_timestamp()
        )
    """))
    if signing_secret is not None:
        connection.execute(text("""
            INSERT INTO public.a84_provenance_signing_secret(key_epoch,secret)
            VALUES (1,:secret)
        """), {"secret": signing_secret})
    connection.execute(text("""
        REVOKE ALL ON public.a84_provenance_signing_secret
        FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime
    """))
    connection.execute(text("""
        ALTER TABLE public.a84_provenance_signing_secret
        ENABLE ROW LEVEL SECURITY
    """))
    connection.execute(text("""
        ALTER TABLE public.a84_provenance_signing_secret
        FORCE ROW LEVEL SECURITY
    """))
    connection.execute(text("""
        CREATE POLICY a84_signing_secret_owner
        ON public.a84_provenance_signing_secret FOR ALL TO kpdesk
        USING (true) WITH CHECK (true)
    """))
    connection.execute(text("""
        CREATE TRIGGER a84_signing_secret_immutable
        BEFORE UPDATE OR DELETE ON public.a84_provenance_signing_secret
        FOR EACH ROW EXECUTE FUNCTION public.kpdesk_review_immutable()
    """))
    # Composite FK also binds immutable source scope and actual reviewer, not UUID alone.
    connection.execute(text("""
        ALTER TABLE public.a72_canonical_checkpoints
        ADD CONSTRAINT a79_checkpoint_full_scope_unique UNIQUE (
          checkpoint_id,organization_id,supplier_id,source_sha256,mapping_sha256,
          taxonomy_version,price_basis,reviewer_user_id
        )
    """))
    connection.execute(text("""
        CREATE TABLE public.a79_catalog_review_provenance (
          organization_id uuid NOT NULL,
          catalog_version_id uuid NOT NULL,
          checkpoint_id uuid NOT NULL,
          supplier_id text NOT NULL,
          original_source_sha256 text NOT NULL,
          mapping_sha256 text NOT NULL,
          canonical_csv_sha256 text NOT NULL,
          taxonomy_version text NOT NULL,
          price_basis text NOT NULL,
          reviewer_user_id uuid NOT NULL,
          created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          PRIMARY KEY (organization_id,catalog_version_id),
          FOREIGN KEY (organization_id,catalog_version_id)
            REFERENCES public.catalog_versions(organization_id,id),
          FOREIGN KEY (
            checkpoint_id,organization_id,supplier_id,original_source_sha256,
            mapping_sha256,taxonomy_version,price_basis,reviewer_user_id
          ) REFERENCES public.a72_canonical_checkpoints (
            checkpoint_id,organization_id,supplier_id,source_sha256,
            mapping_sha256,taxonomy_version,price_basis,reviewer_user_id
          ),
          CHECK(original_source_sha256 ~ '^[0-9a-f]{64}$'),
          CHECK(mapping_sha256 ~ '^[0-9a-f]{64}$'),
          CHECK(canonical_csv_sha256 ~ '^[0-9a-f]{64}$')
        )
    """))
    connection.execute(text("""
        REVOKE ALL ON public.a79_catalog_review_provenance
        FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime
    """))
    connection.execute(text("""
        CREATE TRIGGER a79_provenance_immutable
        BEFORE UPDATE OR DELETE ON public.a79_catalog_review_provenance
        FOR EACH ROW EXECUTE FUNCTION public.kpdesk_review_immutable()
    """))
    connection.execute(text("""
        ALTER TABLE public.a79_catalog_review_provenance ENABLE ROW LEVEL SECURITY
    """))
    connection.execute(text("""
        ALTER TABLE public.a79_catalog_review_provenance FORCE ROW LEVEL SECURITY
    """))
    connection.execute(text("""
        CREATE POLICY a79_schema_owner ON public.a79_catalog_review_provenance
        FOR ALL TO kpdesk USING (true) WITH CHECK (true)
    """))
    connection.execute(text("""
        CREATE FUNCTION public.a79_review_snapshot(
          p_org uuid,p_supplier text,p_source text,p_mapping text,
          p_taxonomy text,p_basis text,p_checkpoint uuid
        ) RETURNS TABLE(reviewer uuid,decisions jsonb)
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        BEGIN
          IF current_setting('kpdesk.session_sha256',true) !~ '^[0-9a-f]{64}$'
             OR NOT public.kpdesk_session_tenant_allowed(p_org,'write')
          THEN RETURN; END IF;
          PERFORM 1 FROM public.a77_current_head h
          WHERE h.organization_id=p_org AND h.supplier_id=p_supplier
            AND h.source_sha256=p_source AND h.mapping_sha256=p_mapping
            AND h.taxonomy_version=p_taxonomy AND h.price_basis=p_basis
            AND h.checkpoint_id=p_checkpoint FOR UPDATE;
          IF NOT FOUND THEN RETURN; END IF;
          RETURN QUERY
          SELECT h.reviewer_user_id,
                 COALESCE(jsonb_agg(r.decision ORDER BY r.source_line),'[]'::jsonb)
          FROM public.a77_current_head h
          JOIN public.a77_current_row r
            ON r.organization_id=h.organization_id
           AND r.supplier_id=h.supplier_id
           AND r.source_sha256=h.source_sha256
           AND r.mapping_sha256=h.mapping_sha256
           AND r.taxonomy_version=h.taxonomy_version
           AND r.price_basis=h.price_basis
          WHERE h.organization_id=p_org AND h.supplier_id=p_supplier
            AND h.source_sha256=p_source AND h.mapping_sha256=p_mapping
            AND h.taxonomy_version=p_taxonomy AND h.price_basis=p_basis
            AND h.checkpoint_id=p_checkpoint
          GROUP BY h.reviewer_user_id,h.checkpoint_id,h.total_count
          HAVING count(*)=max(h.total_count);
        END $$;
    """))
    connection.execute(text("""
        CREATE FUNCTION public.a79_bind_published_review(
          p_org uuid,p_version uuid,p_checkpoint uuid,p_supplier text,
          p_source text,p_mapping text,p_csv text,p_taxonomy text,p_basis text,
          p_reviewer uuid,p_key_epoch bigint,p_signature text
        ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        DECLARE
          existing public.a79_catalog_review_provenance%ROWTYPE;
          actor uuid;
          signing_key bytea;
          expected_signature text;
        BEGIN
          IF current_setting('kpdesk.session_sha256',true) !~ '^[0-9a-f]{64}$'
             OR NOT public.kpdesk_session_tenant_allowed(p_org,'write')
          THEN RETURN false; END IF;
          SELECT s.user_id INTO actor FROM public.auth_sessions s
          WHERE s.token_sha256=current_setting('kpdesk.session_sha256',true)
            AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp();
          IF actor IS DISTINCT FROM p_reviewer
             OR p_signature IS NULL OR p_signature !~ '^[0-9a-f]{64}$'
          THEN RETURN false; END IF;
          SELECT s.secret INTO signing_key
          FROM public.a84_provenance_signing_secret s
          WHERE s.key_epoch=p_key_epoch
            AND s.key_epoch=(SELECT max(key_epoch)
              FROM public.a84_provenance_signing_secret);
          IF signing_key IS NULL THEN RETURN false; END IF;
          expected_signature := encode(public.hmac(
            convert_to(concat_ws(chr(31),
              'kpdesk/a85/review-publish/v1',p_key_epoch::text,
              p_org::text,p_version::text,p_checkpoint::text,
              p_supplier,p_source,p_mapping,p_csv,p_taxonomy,p_basis,
              p_reviewer::text,txid_current()::text,
              current_setting('kpdesk.session_sha256',true)
            ),'UTF8'),signing_key,'sha256'
          ),'hex');
          IF expected_signature IS DISTINCT FROM p_signature THEN
            RETURN false;
          END IF;
          SELECT * INTO existing FROM public.a79_catalog_review_provenance
          WHERE organization_id=p_org AND catalog_version_id=p_version;
          IF FOUND THEN
            RETURN existing.checkpoint_id=p_checkpoint
              AND existing.original_source_sha256=p_source
              AND existing.mapping_sha256=p_mapping
              AND existing.canonical_csv_sha256=p_csv
              AND existing.taxonomy_version=p_taxonomy
              AND existing.price_basis=p_basis
              AND existing.supplier_id=p_supplier
              AND existing.reviewer_user_id=p_reviewer;
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM public.catalog_versions
            WHERE organization_id=p_org AND id=p_version AND source_sha256=p_csv
              AND xmin::text=(
                pg_current_xact_id()::text::numeric % 4294967296
              )::bigint::text
          ) OR NOT EXISTS (
            SELECT 1 FROM public.tenant_catalog_active a
            WHERE a.organization_id=p_org AND a.catalog_version_id=p_version
          ) OR NOT EXISTS (
            SELECT 1 FROM public.a77_current_head
            WHERE organization_id=p_org AND supplier_id=p_supplier
              AND source_sha256=p_source AND mapping_sha256=p_mapping
              AND taxonomy_version=p_taxonomy AND price_basis=p_basis
              AND checkpoint_id=p_checkpoint AND reviewer_user_id=p_reviewer
              AND total_count>0
          ) THEN RETURN false; END IF;
          INSERT INTO public.a79_catalog_review_provenance(
            organization_id,catalog_version_id,checkpoint_id,supplier_id,
            original_source_sha256,mapping_sha256,canonical_csv_sha256,
            taxonomy_version,price_basis,reviewer_user_id
          ) VALUES (
            p_org,p_version,p_checkpoint,p_supplier,p_source,p_mapping,p_csv,
            p_taxonomy,p_basis,p_reviewer
          );
          RETURN true;
        END $$;
    """))
    for signature in (
        "a79_review_snapshot(uuid,text,text,text,text,text,uuid)",
        "a79_bind_published_review(uuid,uuid,uuid,text,text,text,text,text,text,uuid,bigint,text)",
    ):
        connection.execute(text(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC"))
        connection.execute(text(f"GRANT EXECUTE ON FUNCTION public.{signature} TO kpdesk_tenant_runtime"))



def rotate_review_signer(connection: Connection) -> ReviewSigningCredential:
    """Rotate the isolated signer in an owner transaction; never mutate old keys.

    A stale worker fails closed as soon as this transaction commits. Production
    rotation/distribution is intentionally NOT implemented by this test helper.
    """
    if not connection.in_transaction() or not is_live_accounts_http_clone(connection):
        raise RuntimeError("Signing rotation requires an isolated clone transaction.")
    if connection.scalar(text("SELECT current_user")) != "kpdesk":
        raise RuntimeError("Signing rotation requires the clone schema owner.")
    if connection.scalar(text("SELECT version_num FROM alembic_version")) != (
        "0030_tenant_llm_cache"
    ):
        raise RuntimeError("Signing rotation requires expected clone schema.")
    connection.execute(text("""
        SELECT pg_advisory_xact_lock(
          hashtextextended('kpdesk/a85/review-signer-rotation', 0)
        )
    """))
    secret = secrets.token_bytes(32)
    next_epoch = connection.scalar(text("""
        INSERT INTO public.a84_provenance_signing_secret(key_epoch,secret)
        SELECT coalesce(max(key_epoch),0)+1,:secret
        FROM public.a84_provenance_signing_secret
        RETURNING key_epoch
    """), {"secret": secret})
    if not isinstance(next_epoch, int):
        raise TypeError("Signing rotation did not return a valid key epoch.")
    return ReviewSigningCredential(epoch=next_epoch, key=secret)
