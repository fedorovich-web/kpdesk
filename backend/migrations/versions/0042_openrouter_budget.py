"""Shared OpenRouter budget; historical inference audit remains intact."""
from alembic import op
from sqlalchemy import text

from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone
from kpdesk.db.provider_budget_schema import CACHE_CLAIM, create_provider_budget

revision = '0042_openrouter_budget'
down_revision = '0041_runtime_readiness'
branch_labels = None
depends_on = None


def upgrade() -> None:
    config = op.get_context().config
    if config is None or config.attributes.get('kpdesk_openrouter_budget_0042_approved') is not True:
        raise RuntimeError('OpenRouter budget migration requires explicit approval')
    connection = op.get_bind()
    database, role, previous = connection.execute(text(
        'SELECT current_database(),current_user,(SELECT version_num FROM public.alembic_version)'
    )).one()
    if (not connection.in_transaction() or role != 'kpdesk' or previous != down_revision
            or (database != 'kpdesk' and not is_live_accounts_http_clone(connection))):
        raise RuntimeError('Unsafe OpenRouter budget migration target')
    connection.execute(text("SET LOCAL lock_timeout='5s'"))
    connection.execute(text('ALTER TABLE public.tenant_llm_attempts DROP CONSTRAINT ck_llm_model'))
    connection.execute(text("""ALTER TABLE public.tenant_llm_attempts ADD CONSTRAINT ck_llm_model
        CHECK(model IN ('openai/gpt-oss-120b','openai/gpt-oss-20b','qwen/qwen3.8-27b',
                        'nvidia/nemotron-3-ultra-550b-a55b:free'))"""))
    connection.execute(text(_RESERVE))
    connection.execute(text(CACHE_CLAIM))
    create_provider_budget(connection)


def downgrade() -> None:
    raise RuntimeError('Provider audit retention requires a separately reviewed migration')


# Reviewed 0019 reservation logic, with only the model and lease changed.
_RESERVE = """CREATE OR REPLACE FUNCTION public.kpdesk_llm_reserve(
            p_org uuid,p_id uuid,p_model text,p_chars integer
        ) RETURNS text LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        DECLARE
            v_user uuid;
            v_now timestamptz;
            v_count bigint;
            v_chars bigint;
        BEGIN
            IF p_org IS NULL OR p_id IS NULL OR p_model IS NULL
                OR p_model <> 'nvidia/nemotron-3-ultra-550b-a55b:free' OR p_chars IS NULL OR p_chars NOT BETWEEN 1 AND 12000
            THEN RETURN 'invalid'; END IF;
            IF current_setting('kpdesk.session_sha256',true)
                   !~ '^[0-9a-f]{64}$'
            THEN RETURN 'forbidden'; END IF;
            -- The caller binds auth session + CSRF into a transaction-local
            -- setting via the existing A14 tenant authorization function.
            -- Serializing by org protects call quotas from concurrent races.
            PERFORM 1 FROM public.organizations o
            WHERE o.id=p_org AND o.status='active' FOR UPDATE;
            IF NOT FOUND OR NOT public.kpdesk_session_tenant_allowed(
                p_org,'write'
            ) THEN RETURN 'forbidden'; END IF;
            SELECT s.user_id INTO v_user FROM public.auth_sessions s
            JOIN public.users u ON u.id=s.user_id
            JOIN public.organization_memberships m
              ON m.user_id=s.user_id AND m.organization_id=p_org
            WHERE s.token_sha256=current_setting('kpdesk.session_sha256',true)
              AND s.revoked_at IS NULL
              AND s.expires_at>clock_timestamp()
              AND u.status='active' AND u.email_verified_at IS NOT NULL
              AND m.status='active' AND m.role IN ('owner','editor');
            IF v_user IS NULL THEN RETURN 'forbidden'; END IF;
            v_now:=clock_timestamp();
            SELECT count(*),COALESCE(sum(input_chars),0)
                INTO v_count,v_chars
            FROM public.tenant_llm_attempts
            WHERE organization_id=p_org
              AND created_at>v_now-interval '1 day';
            IF v_count>=40 OR v_chars+p_chars>120000
            THEN RETURN 'organization_quota'; END IF;
            SELECT count(*) INTO v_count
            FROM public.tenant_llm_attempts
            WHERE user_id=v_user
              AND created_at>v_now-interval '1 hour';
            IF v_count>=6 THEN RETURN 'user_quota'; END IF;
            SELECT count(*) INTO v_count
            FROM public.tenant_llm_attempts
            WHERE organization_id=p_org AND status='reserved'
              AND lease_expires_at>v_now;
            IF v_count>=2 THEN RETURN 'organization_busy'; END IF;
            SELECT count(*) INTO v_count
            FROM public.tenant_llm_attempts
            WHERE user_id=v_user AND status='reserved'
              AND lease_expires_at>v_now;
            IF v_count>=1 THEN RETURN 'user_busy'; END IF;
            INSERT INTO public.tenant_llm_attempts(
                id,organization_id,user_id,model,input_chars,
                lease_expires_at
            ) VALUES(p_id,p_org,v_user,p_model,p_chars,v_now+interval '300 seconds');
            RETURN 'reserved';
        END $$;
"""
