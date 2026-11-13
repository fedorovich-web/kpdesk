"""Read-only release schema check and optional built frontend, never legacy APIs."""
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import Engine, text

REQUIRED_SCHEMA = '0042_openrouter_budget'
SCHEMA_CHECK = text('''SELECT current_user,
    public.kpdesk_runtime_schema_revision(),
    to_regprocedure('public.kpdesk_company_profile_read(uuid)') IS NOT NULL
      AND to_regprocedure('public.kpdesk_company_profile_save(uuid,jsonb,integer)') IS NOT NULL,
    to_regprocedure('public.kpdesk_training_feedback_retry(uuid,uuid,integer,text,text,text)') IS NOT NULL
      AND to_regprocedure('public.kpdesk_training_feedback_save(uuid,jsonb,text,text)') IS NOT NULL
      AND to_regprocedure('public.kpdesk_training_feedback_withdraw(uuid,uuid,uuid)') IS NOT NULL
      AND to_regprocedure('public.kpdesk_provider_reserve(uuid,uuid,text,uuid,uuid,integer,integer,integer,integer,integer)') IS NOT NULL
      AND to_regprocedure('public.kpdesk_provider_finish(uuid,uuid,uuid,uuid,text,integer,integer,integer,integer,integer)') IS NOT NULL
      AND to_regprocedure('public.kpdesk_provider_close(uuid,uuid,uuid,boolean,boolean)') IS NOT NULL,
    (SELECT count(*)=3 FROM pg_catalog.pg_class WHERE relrowsecurity AND relforcerowsecurity
      AND oid IN (to_regclass('public.organization_proposal_profiles'),
                  to_regclass('public.training_feedback_records'),
                  to_regclass('public.training_feedback_withdrawals')))
''')


def verify_release_schema(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text('SET TRANSACTION READ ONLY'))
        available: object = connection.execute(text("SELECT to_regprocedure('public.kpdesk_runtime_schema_revision()') IS NOT NULL")).scalar_one()
        if available is not True:
            raise RuntimeError('Production database schema is not ready; runtime metadata function is missing.')
        result = connection.execute(SCHEMA_CHECK).one()
        if result[1] in ('0043_proposal_shares', '0044_universal_products', '0045_registry_retirement', '0046_provider_authority', '0047_auth_reset_serialization'):
            from kpdesk.quotes.sharing import sharing_available

            if not sharing_available(connection) or connection.scalar(text('''SELECT relrowsecurity AND relforcerowsecurity
                FROM pg_catalog.pg_class WHERE oid=to_regclass('public.proposal_shares')''')) is not True:
                raise RuntimeError('Production database schema is not ready; proposal sharing gateway is incomplete.')
        if result[1] in ('0044_universal_products', '0045_registry_retirement', '0046_provider_authority', '0047_auth_reset_serialization'):
            from kpdesk.db.universal_schema import universal_available

            if not universal_available(connection):
                raise RuntimeError('Production database schema is not ready; universal gateway is incomplete.')
        if result[1] in ('0045_registry_retirement', '0046_provider_authority', '0047_auth_reset_serialization'):
            from kpdesk.db.supplier_registry_schema import registry_retired

            if not registry_retired(connection):
                raise RuntimeError('Production database schema is not ready; retired registry privileges remain.')
    if (result[1] not in (REQUIRED_SCHEMA, '0043_proposal_shares', '0044_universal_products', '0045_registry_retirement', '0046_provider_authority', '0047_auth_reset_serialization')
            or (result[0], *result[2:]) != ('kpdesk_tenant_runtime', True, True, True)):
        raise RuntimeError('Production database schema is not ready; reviewed migrations must be applied separately.')


def mount_built_frontend(app: FastAPI, directory: str) -> None:
    candidate = Path(directory)
    if not candidate.is_absolute() or not candidate.is_dir():
        raise RuntimeError('Frontend build directory must be an existing absolute directory.')
    root = candidate.resolve()
    if not (root/'index.html').is_file() or not (root/'404.html').is_file():
        raise RuntimeError('Frontend build is incomplete.')

    def safe_file(relative: str) -> Path | None:
        if '\\' in relative or any(part.startswith('.') for part in relative.split('/') if part):
            return None
        try:
            path = (root/relative).resolve()
            return path if path.is_relative_to(root) and path.is_file() else None
        except (OSError, ValueError, RuntimeError):
            return None

    if safe_file('index.html') is None or safe_file('404.html') is None:
        raise RuntimeError('Frontend required pages must stay within the build directory.')

    @app.get('/{path:path}', include_in_schema=False)
    def frontend(path: str) -> FileResponse:
        if path == 'api' or path.startswith('api/'):
            raise HTTPException(404)
        normalized = path.rstrip('/')
        private = normalized in ('app', 'auth', 'proposal') or normalized.startswith(('app/', 'auth/'))
        if not normalized:
            file = safe_file('index.html')
        elif normalized == 'proposal':
            file = safe_file('proposal/index.html')
        elif private:
            file = safe_file('app/index.html' if normalized == 'app' or normalized.startswith('app/') else 'auth/login/index.html')
        else:
            file = safe_file(normalized) or safe_file(normalized+'/index.html')
        if file is None:
            error_page=safe_file('404.html')
            if error_page is None:
                raise HTTPException(404)
            return FileResponse(error_page, status_code=404, headers={'X-Robots-Tag': 'noindex, nofollow'})
        headers = {'Cache-Control': 'no-store', 'X-Robots-Tag': 'noindex, nofollow'} if private else {}
        return FileResponse(file, headers=headers)
