"""Restricted, transactional user moderation for KPDesk service owners."""
from sqlalchemy import Connection, text

from kpdesk.db.service_admin_schema import add_user_management_projection, service_admin_available

MODERATION_REVISION = '0052_service_admin_users'
MODERATION_SIGNATURE = 'public.kpdesk_service_admin_user_action(text,text,uuid,text)'

_AUDIT = """
CREATE TABLE public.service_admin_user_audit (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_id uuid NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
    target_id uuid NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
    action text NOT NULL CHECK (action IN ('block','unblock','revoke_sessions')),
    previous_status text NOT NULL CHECK (previous_status IN ('active','pending','disabled')),
    new_status text NOT NULL CHECK (new_status IN ('active','pending','disabled')),
    sessions_revoked bigint NOT NULL CHECK (sessions_revoked >= 0),
    occurred_at timestamptz NOT NULL,
    CHECK (actor_id <> target_id)
)
"""

_MODERATE = """
CREATE FUNCTION public.kpdesk_service_admin_user_action(
    p_hash text, p_csrf text, p_target uuid, p_action text
) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE
    actor uuid;
    actor_expires timestamptz;
    actor_csrf text;
    old_status text;
    new_status text;
    previous_status text;
    grant_revoked timestamptz;
    observed timestamptz;
    revoked_count bigint := 0;
BEGIN
    IF p_hash IS NULL OR p_hash !~ '^[0-9a-f]{64}$' THEN
        RETURN jsonb_build_object('access','invalid_session');
    END IF;
    IF p_csrf IS NULL OR p_csrf !~ '^[0-9a-f]{64}$' THEN
        RETURN jsonb_build_object('access','invalid_csrf');
    END IF;
    IF p_target IS NULL OR p_action IS NULL OR p_action NOT IN ('block','unblock','revoke_sessions') THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid service admin action';
    END IF;

    -- Follow the password reset and Yandex lock order: user first, session second.
    SELECT u.id INTO actor FROM public.auth_sessions s JOIN public.users u ON u.id=s.user_id
    WHERE s.token_sha256=p_hash AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp()
      AND u.status='active' AND u.email_verified_at IS NOT NULL FOR SHARE OF u;
    IF NOT FOUND THEN RETURN jsonb_build_object('access','invalid_session'); END IF;
    SELECT s.expires_at,s.csrf_sha256 INTO actor_expires,actor_csrf FROM public.auth_sessions s
    WHERE s.token_sha256=p_hash AND s.user_id=actor AND s.revoked_at IS NULL FOR SHARE OF s;
    IF NOT FOUND OR actor_expires<=clock_timestamp() THEN
        RETURN jsonb_build_object('access','invalid_session');
    END IF;
    IF actor_csrf<>p_csrf THEN RETURN jsonb_build_object('access','invalid_csrf'); END IF;
    PERFORM 1 FROM public.service_admin_grants WHERE user_id=actor AND revoked_at IS NULL FOR SHARE;
    IF NOT FOUND THEN RETURN jsonb_build_object('access','forbidden'); END IF;

    -- Reject obvious protected targets before taking any lock on another owner.
    IF p_target=actor OR EXISTS (SELECT 1 FROM public.service_admin_grants
            WHERE user_id=p_target AND revoked_at IS NULL) THEN
        RETURN jsonb_build_object('access','protected');
    END IF;
    SELECT u.status,u.service_admin_previous_status INTO old_status,previous_status
    FROM public.users u WHERE u.id=p_target FOR UPDATE;
    IF NOT FOUND THEN RETURN jsonb_build_object('access','missing'); END IF;
    -- The user lock blocks a new grant's foreign key check. Lock and recheck an existing grant too.
    SELECT g.revoked_at INTO grant_revoked FROM public.service_admin_grants g
    WHERE g.user_id=p_target FOR SHARE;
    IF FOUND AND grant_revoked IS NULL THEN RETURN jsonb_build_object('access','protected'); END IF;

    observed := clock_timestamp();
    IF actor_expires<=observed THEN RETURN jsonb_build_object('access','invalid_session'); END IF;
    new_status := old_status;
    IF p_action='block' THEN
        IF old_status NOT IN ('active','pending') THEN RETURN jsonb_build_object('access','conflict'); END IF;
        previous_status := old_status;
        new_status := 'disabled';
        UPDATE public.users SET status=new_status,service_admin_previous_status=previous_status WHERE id=p_target;
    ELSIF p_action='unblock' THEN
        IF previous_status IS NULL THEN RETURN jsonb_build_object('access','conflict'); END IF;
        new_status := previous_status;
        previous_status := NULL;
        UPDATE public.users SET status=new_status,service_admin_previous_status=NULL WHERE id=p_target;
    END IF;
    IF p_action IN ('block','revoke_sessions') THEN
        UPDATE public.auth_sessions SET revoked_at=observed
        WHERE user_id=p_target AND revoked_at IS NULL AND expires_at>observed;
        GET DIAGNOSTICS revoked_count = ROW_COUNT;
    END IF;
    INSERT INTO public.service_admin_user_audit
        (actor_id,target_id,action,previous_status,new_status,sessions_revoked,occurred_at)
    VALUES (actor,p_target,p_action,old_status,new_status,revoked_count,observed);
    RETURN jsonb_build_object('access','allowed','data',jsonb_build_object(
        'id',p_target,'status',new_status,'is_blocked',previous_status IS NOT NULL,'sessions_revoked',revoked_count));
END
$$
"""


def create_service_admin_user_schema(connection: Connection) -> None:
    for statement in (
        """ALTER TABLE public.users ADD COLUMN service_admin_previous_status text
           CHECK (service_admin_previous_status IS NULL OR
                  (status='disabled' AND service_admin_previous_status IN ('active','pending')))""",
        _AUDIT,
        'ALTER TABLE public.service_admin_user_audit ENABLE ROW LEVEL SECURITY',
        'ALTER TABLE public.service_admin_user_audit FORCE ROW LEVEL SECURITY',
        'CREATE POLICY service_admin_audit_owner ON public.service_admin_user_audit TO kpdesk USING(true) WITH CHECK(true)',
        'REVOKE ALL ON public.service_admin_user_audit FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime',
        _MODERATE,
        f'REVOKE ALL ON FUNCTION {MODERATION_SIGNATURE} FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime',
        f'GRANT EXECUTE ON FUNCTION {MODERATION_SIGNATURE} TO kpdesk_tenant_runtime',
    ):
        connection.execute(text(statement))
    add_user_management_projection(connection)


def service_admin_users_available(connection: Connection) -> bool:
    return service_admin_available(connection) and connection.scalar(text(f"""
        SELECT EXISTS (
            SELECT FROM pg_catalog.pg_class c WHERE c.oid=to_regclass('public.service_admin_user_audit')
              AND c.relkind='r' AND c.relrowsecurity AND c.relforcerowsecurity
              AND pg_get_userbyid(c.relowner)='kpdesk'
              AND NOT EXISTS (SELECT FROM aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) a
                              WHERE a.grantee<>c.relowner)
              AND NOT EXISTS (SELECT FROM pg_catalog.pg_attribute att, LATERAL aclexplode(att.attacl) a
                              WHERE att.attrelid=c.oid AND a.grantee<>c.relowner)
              AND (SELECT count(*)=1 FROM pg_catalog.pg_policy pol WHERE pol.polrelid=c.oid)
              AND EXISTS (SELECT FROM pg_catalog.pg_policy pol WHERE pol.polrelid=c.oid
                          AND pol.polroles=ARRAY[c.relowner]::oid[] AND pol.polcmd='*' AND pol.polpermissive
                          AND pg_get_expr(pol.polqual,pol.polrelid)='true'
                          AND pg_get_expr(pol.polwithcheck,pol.polrelid)='true')
        ) AND EXISTS (
            SELECT FROM pg_catalog.pg_proc p WHERE p.oid=to_regprocedure('{MODERATION_SIGNATURE}')
              AND p.prosecdef AND pg_get_userbyid(p.proowner)='kpdesk'
              AND p.proconfig=ARRAY['search_path=pg_catalog, pg_temp']::text[]
              AND has_function_privilege('kpdesk_tenant_runtime',p.oid,'EXECUTE')
              AND NOT has_function_privilege('kpdesk_auth_runtime',p.oid,'EXECUTE')
              AND NOT has_function_privilege('kpdesk_app',p.oid,'EXECUTE')
              AND NOT EXISTS (SELECT FROM aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
                              WHERE a.grantee<>p.proowner
                                AND (a.grantee<>'kpdesk_tenant_runtime'::regrole OR a.is_grantable))
        )
    """)) is True
