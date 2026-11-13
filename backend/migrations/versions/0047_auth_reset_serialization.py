"""Serialize credential verification with password reset, under explicit approval.

The credential function holds the user's row lock until CommittedLogin commits
its session. A concurrent reset must therefore either precede that verification
or revoke the newly committed session. No account/session rows or ACLs change
in this migration. Downgrade deliberately restores the vulnerable lookup and is
a security rollback: invoke it only with explicit rollback authorization, using
the same approval attribute and exact current revision guard.
"""
from alembic import op
from sqlalchemy import text

from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone

revision = '0047_auth_reset_serialization'
down_revision = '0046_provider_authority'
branch_labels = None
depends_on = None

_APPROVAL = 'kpdesk_auth_reset_serialization_0047_approved'
_LOOKUP = 'FROM public.users u WHERE u.email=p_email;'
_LOCKED_LOOKUP = 'FROM public.users u WHERE u.email=p_email FOR UPDATE OF u;'
_HEADER = (
    'CREATE OR REPLACE FUNCTION public.kpdesk_auth_credential(p_email text, p_peer text)'
)
# Frozen body installed by 0014. Refuse to overwrite any later/local function
# variant instead of reconstructing its rate limits or security behavior.
_ORIGINAL_SOURCE = """
        DECLARE
            observed timestamptz := clock_timestamp();
            account_bucket text;
            peer_bucket text;
            account_count integer;
            peer_count integer;
        BEGIN
            IF p_email IS NULL OR length(p_email)>254
               OR p_peer IS NULL OR length(p_peer)>64
            THEN
                RAISE EXCEPTION 'invalid authentication request';
            END IF;
            account_bucket := 'a:' || encode(sha256(convert_to(p_email,'UTF8')),'hex');
            peer_bucket := 'p:' || encode(sha256(convert_to(p_peer,'UTF8')),'hex');
            INSERT INTO public.auth_login_limits(bucket,window_start,attempts)
            VALUES (LEAST(account_bucket,peer_bucket),observed,1)
            ON CONFLICT (bucket) DO UPDATE SET
                attempts=CASE WHEN public.auth_login_limits.window_start
                    < observed - interval '15 minutes' THEN 1
                    ELSE public.auth_login_limits.attempts + 1 END,
                window_start=CASE WHEN public.auth_login_limits.window_start
                    < observed - interval '15 minutes' THEN observed
                    ELSE public.auth_login_limits.window_start END;
            INSERT INTO public.auth_login_limits(bucket,window_start,attempts)
            VALUES (GREATEST(account_bucket,peer_bucket),observed,1)
            ON CONFLICT (bucket) DO UPDATE SET
                attempts=CASE WHEN public.auth_login_limits.window_start
                    < observed - interval '15 minutes' THEN 1
                    ELSE public.auth_login_limits.attempts + 1 END,
                window_start=CASE WHEN public.auth_login_limits.window_start
                    < observed - interval '15 minutes' THEN observed
                    ELSE public.auth_login_limits.window_start END;
            SELECT attempts INTO account_count FROM public.auth_login_limits
            WHERE bucket=account_bucket;
            SELECT attempts INTO peer_count FROM public.auth_login_limits
            WHERE bucket=peer_bucket;
            IF account_count>6 OR peer_count>30 THEN
                RETURN QUERY SELECT NULL::uuid,NULL::text,false,true;
                RETURN;
            END IF;
            RETURN QUERY SELECT u.id,u.password_hash::text,
                (u.status='active' AND u.email_verified_at IS NOT NULL),false
            FROM public.users u WHERE u.email=p_email;
        END """

_INSTALLED = text("""
    SELECT p.prosrc AS source,pg_get_functiondef(p.oid) AS definition,
           p.prosecdef AS security_definer,p.proconfig AS configuration,
           l.lanname AS language,r.rolname AS owner,
           pg_get_function_identity_arguments(p.oid) AS identity_arguments,
           pg_get_function_result(p.oid) AS result
    FROM pg_catalog.pg_proc p
    JOIN pg_catalog.pg_language l ON l.oid=p.prolang
    JOIN pg_catalog.pg_roles r ON r.oid=p.proowner
    WHERE p.oid=to_regprocedure('public.kpdesk_auth_credential(text,text)')
""")


def _replace(*, undo: bool) -> None:
    config = op.get_context().config
    if config is None or config.attributes.get(_APPROVAL) is not True:
        raise RuntimeError('Auth reset serialization migration requires explicit approval')
    connection = op.get_bind()
    if not connection.in_transaction():
        raise RuntimeError('Unsafe auth reset serialization migration target')
    database, role, previous = connection.execute(text(
        'SELECT current_database(),current_user,(SELECT version_num FROM public.alembic_version)',
    )).one()
    expected_revision = revision if undo else down_revision
    if (role != 'kpdesk' or previous != expected_revision
            or (database != 'kpdesk' and not is_live_accounts_http_clone(connection))):
        raise RuntimeError('Unsafe auth reset serialization migration target')
    connection.execute(text("SET LOCAL statement_timeout='10s'"))
    connection.execute(text("SET LOCAL lock_timeout='5s'"))
    row = connection.execute(_INSTALLED).mappings().one_or_none()
    old, new = (_LOCKED_LOOKUP, _LOOKUP) if undo else (_LOOKUP, _LOCKED_LOOKUP)
    expected_source = _ORIGINAL_SOURCE.replace(_LOOKUP, _LOCKED_LOOKUP) if undo else _ORIGINAL_SOURCE
    if (row is None or row['source'].strip() != expected_source.strip()
            or row['security_definer'] is not True
            or row['configuration'] != ['search_path=pg_catalog, pg_temp']
            or row['language'] != 'plpgsql' or row['owner'] != 'kpdesk'
            or row['identity_arguments'] != 'p_email text, p_peer text'
            or row['result'] != 'TABLE(identity_id uuid, secret_hash text, usable boolean, limited boolean)'
            or not row['definition'].startswith(_HEADER)
            or row['definition'].count(row['source']) != 1
            or row['definition'].count(old) != 1):
        raise RuntimeError('Unexpected installed authentication gateway; refusing replacement')
    # pg_get_functiondef retains the installed signature and attributes;
    # CREATE OR REPLACE additionally retains ownership and ACLs. Only this final
    # SELECT changes; the full prior body/rate limits were checked above.
    connection.execute(text(row['definition'].replace(old, new, 1)))
    target = down_revision if undo else revision
    connection.execute(text(f"""CREATE OR REPLACE FUNCTION public.kpdesk_runtime_schema_revision()
        RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp
        AS $$ SELECT '{target}'::text $$"""))


def upgrade() -> None:
    _replace(undo=False)


def downgrade() -> None:
    """Explicitly approved security rollback; never a default recovery action."""
    _replace(undo=True)
