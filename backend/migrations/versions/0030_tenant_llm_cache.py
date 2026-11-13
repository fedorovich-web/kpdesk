"""A51: privacy-minimized tenant-scoped Groq result reuse, bounded lease/TTL.

Revision ID: 0030_tenant_llm_cache
Revises: 0029_manager_decisions_read

No raw request text. Cache hits still get a fresh quota-ledger attempt and
review ID. A short pending lease prevents concurrent duplicate provider calls.
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op

revision = "0030_tenant_llm_cache"
down_revision = "0029_manager_decisions_read"
branch_labels = None
depends_on = None
_CLONE = re.compile(r"kpdesk_accounts_http_[0-9a-f]{12}\Z")


def upgrade() -> None:
    connection = op.get_bind()
    db = connection.scalar(sa.text("SELECT current_database()"))
    if db != "kpdesk":
        if not isinstance(db, str) or _CLONE.fullmatch(db) is None:
            raise RuntimeError("A51 requires KPDesk or marked isolated clone.")
        marker = connection.scalar(sa.text("""
            SELECT shobj_description(oid,'pg_database')
            FROM pg_database WHERE datname=:name
        """), {"name": db})
        if marker != "kpdesk:accounts-http:" + db.rsplit("_", 1)[-1]:
            raise RuntimeError("A51 clone provenance mismatch.")
    if connection.scalar(sa.text("SELECT current_user")) != "kpdesk":
        raise RuntimeError("A51 migration requires PostgreSQL schema owner.")
    if connection.scalar(sa.text("SELECT version_num FROM alembic_version")) != (
        "0029_manager_decisions_read"
    ):
        raise RuntimeError("A51 requires exact migration 0029.")
    if connection.scalar(sa.text(
        "SELECT to_regclass('public.tenant_llm_extraction_cache')"
    )) is not None:
        raise RuntimeError("A51 cache already exists.")

    op.execute("""
        CREATE TABLE public.tenant_llm_extraction_cache (
          organization_id uuid NOT NULL
            REFERENCES public.organizations(id) ON DELETE RESTRICT,
          request_hmac char(64) NOT NULL
            CHECK (request_hmac ~ '^[0-9a-f]{64}$'),
          model varchar(50) NOT NULL,
          parser_version varchar(32) NOT NULL,
          status varchar(8) NOT NULL
            CHECK (status IN ('pending','ready')),
          owner_attempt_id uuid NOT NULL
            REFERENCES public.tenant_llm_attempts(id) ON DELETE RESTRICT,
          extracted_payload bytea,
          created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          expires_at timestamptz NOT NULL,
          PRIMARY KEY (organization_id,request_hmac),
          CONSTRAINT ck_llm_cache_state CHECK (
            (status='pending' AND extracted_payload IS NULL)
            OR (status='ready' AND extracted_payload IS NOT NULL
                AND octet_length(extracted_payload) BETWEEN 1 AND 65536)
          ),
          CONSTRAINT ck_llm_cache_ttl CHECK (
            expires_at > created_at AND
            expires_at <= created_at+interval '15 minutes'
          )
        )
    """)
    op.execute("""
        CREATE INDEX ix_llm_cache_expiry
        ON public.tenant_llm_extraction_cache(expires_at)
    """)
    op.execute("""
        REVOKE ALL ON public.tenant_llm_extraction_cache
        FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime
    """)
    op.execute("""
        ALTER TABLE public.tenant_llm_extraction_cache
        ENABLE ROW LEVEL SECURITY
    """)
    op.execute("""
        CREATE POLICY kpdesk_cache_owner ON public.tenant_llm_extraction_cache
        FOR ALL TO kpdesk USING (true) WITH CHECK (true)
    """)
    op.execute("""
        ALTER TABLE public.tenant_llm_extraction_cache
        FORCE ROW LEVEL SECURITY
    """)

    op.execute("""
      CREATE FUNCTION public.kpdesk_llm_cache_claim(
        p_org uuid,p_attempt uuid,p_hash text,p_model text,p_version text
      ) RETURNS TABLE(cache_status text, cache_payload bytea)
      LANGUAGE plpgsql SECURITY DEFINER
      SET search_path=pg_catalog,pg_temp AS $$
      DECLARE v_actor uuid;
              v_row public.tenant_llm_extraction_cache%ROWTYPE;
              v_now timestamptz;
      BEGIN
        IF p_org IS NULL OR p_attempt IS NULL
           OR p_hash IS NULL OR p_hash !~ '^[0-9a-f]{64}$'
           OR p_version <> 'kpdesk_request_v3'
           OR p_model NOT IN (
             'openai/gpt-oss-120b','openai/gpt-oss-20b','qwen/qwen3.8-27b'
           )
           OR NOT public.kpdesk_session_tenant_allowed(p_org,'write')
        THEN RETURN QUERY SELECT 'forbidden'::text,NULL::bytea; RETURN; END IF;
        SELECT s.user_id INTO v_actor FROM public.auth_sessions s
        JOIN public.users u ON u.id=s.user_id
        WHERE s.token_sha256=current_setting('kpdesk.session_sha256',true)
          AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp()
          AND u.status='active' AND u.email_verified_at IS NOT NULL;
        IF v_actor IS NULL OR NOT EXISTS(
          SELECT 1 FROM public.tenant_llm_attempts a
          WHERE a.id=p_attempt AND a.organization_id=p_org
            AND a.user_id=v_actor AND a.status='reserved'
            AND a.model=p_model AND a.lease_expires_at>clock_timestamp()
        ) THEN
          RETURN QUERY SELECT 'forbidden'::text,NULL::bytea; RETURN;
        END IF;
        v_now:=clock_timestamp();
        -- A bounded, lock-skipping opportunistic cleanup; stale entries
        -- can also be purged by owner-operated maintenance after expiry.
        WITH stale AS (
          SELECT c.organization_id,c.request_hmac
          FROM public.tenant_llm_extraction_cache c
          WHERE c.expires_at<=v_now
          ORDER BY c.expires_at LIMIT 100
          FOR UPDATE SKIP LOCKED
        )
        DELETE FROM public.tenant_llm_extraction_cache c
        USING stale s
        WHERE c.organization_id=s.organization_id
          AND c.request_hmac=s.request_hmac;
        INSERT INTO public.tenant_llm_extraction_cache(
          organization_id,request_hmac,model,parser_version,status,
          owner_attempt_id,created_at,expires_at
        ) VALUES (
          p_org,p_hash,p_model,p_version,'pending',
          p_attempt,v_now,v_now+interval '45 seconds'
        ) ON CONFLICT DO NOTHING;
        SELECT * INTO v_row FROM public.tenant_llm_extraction_cache c
        WHERE c.organization_id=p_org AND c.request_hmac=p_hash
        FOR UPDATE;
        IF v_row.status='ready' AND v_row.expires_at>v_now THEN
          RETURN QUERY SELECT 'hit'::text,v_row.extracted_payload; RETURN;
        END IF;
        IF v_row.status='pending' AND v_row.expires_at>v_now
          AND v_row.owner_attempt_id<>p_attempt
        THEN
          RETURN QUERY SELECT 'busy'::text,NULL::bytea; RETURN;
        END IF;
        UPDATE public.tenant_llm_extraction_cache c SET
          model=p_model,parser_version=p_version,status='pending',
          owner_attempt_id=p_attempt,extracted_payload=NULL,
          created_at=v_now,expires_at=v_now+interval '45 seconds'
        WHERE c.organization_id=p_org AND c.request_hmac=p_hash;
        RETURN QUERY SELECT 'miss'::text,NULL::bytea;
      END $$;
    """)
    op.execute("""
      CREATE FUNCTION public.kpdesk_llm_cache_publish(
        p_org uuid,p_attempt uuid,p_hash text,p_payload bytea
      ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
      SET search_path=pg_catalog,pg_temp AS $$
      DECLARE v_actor uuid;
      BEGIN
        IF p_org IS NULL OR p_attempt IS NULL OR
          p_hash IS NULL OR p_hash !~ '^[0-9a-f]{64}$'
          OR p_payload IS NULL OR octet_length(p_payload) NOT BETWEEN 1 AND 65536
          OR NOT public.kpdesk_session_tenant_allowed(p_org,'write')
        THEN RETURN FALSE; END IF;
        SELECT s.user_id INTO v_actor FROM public.auth_sessions s
        JOIN public.users u ON u.id=s.user_id
        WHERE s.token_sha256=current_setting('kpdesk.session_sha256',true)
          AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp()
          AND u.status='active' AND u.email_verified_at IS NOT NULL;
        IF v_actor IS NULL OR NOT EXISTS(
          SELECT 1 FROM public.tenant_llm_attempts a
          WHERE a.id=p_attempt AND a.organization_id=p_org
            AND a.user_id=v_actor AND a.status='success'
            AND a.source_sha256 ~ '^[0-9a-f]{64}$'
            AND a.preview_sha256 ~ '^[0-9a-f]{64}$'
            AND a.created_at>clock_timestamp()-interval '1 hour'
        ) THEN RETURN FALSE; END IF;
        UPDATE public.tenant_llm_extraction_cache c SET
          status='ready',extracted_payload=p_payload,
          expires_at=clock_timestamp()+interval '14 minutes'
        WHERE c.organization_id=p_org AND c.request_hmac=p_hash
          AND c.owner_attempt_id=p_attempt
          AND c.status='pending' AND c.expires_at>clock_timestamp();
        RETURN FOUND;
      END $$;
    """)
    op.execute("""
      CREATE FUNCTION public.kpdesk_llm_cache_release(
        p_org uuid,p_attempt uuid,p_hash text
      ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
      SET search_path=pg_catalog,pg_temp AS $$
      DECLARE v_actor uuid;
      BEGIN
        IF p_org IS NULL OR p_attempt IS NULL OR p_hash IS NULL
          OR p_hash !~ '^[0-9a-f]{64}$'
          OR NOT public.kpdesk_session_tenant_allowed(p_org,'write')
        THEN RETURN FALSE; END IF;
        SELECT s.user_id INTO v_actor FROM public.auth_sessions s
        WHERE s.token_sha256=current_setting('kpdesk.session_sha256',true)
          AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp();
        IF NOT EXISTS(
          SELECT 1 FROM public.tenant_llm_attempts a
          WHERE a.id=p_attempt AND a.organization_id=p_org
            AND a.user_id=v_actor
        ) THEN RETURN FALSE; END IF;
        DELETE FROM public.tenant_llm_extraction_cache c
        WHERE c.organization_id=p_org AND c.request_hmac=p_hash
          AND c.owner_attempt_id=p_attempt AND c.status='pending';
        RETURN FOUND;
      END $$;
    """)
    for signature in (
        "kpdesk_llm_cache_claim(uuid,uuid,text,text,text)",
        "kpdesk_llm_cache_publish(uuid,uuid,text,bytea)",
        "kpdesk_llm_cache_release(uuid,uuid,text)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
        op.execute(
            f"GRANT EXECUTE ON FUNCTION public.{signature} TO kpdesk_tenant_runtime"
        )


def downgrade() -> None:
    raise RuntimeError("A51 cached customer-derived data requires reviewed retention handling.")
