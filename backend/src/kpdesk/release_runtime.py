"""Read-only release schema check and optional built frontend, never legacy APIs."""
import re
from mimetypes import guess_type
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import Engine, text

# Vite emits eight-character fingerprints in built JS/CSS filenames.
# Only fingerprinted URLs may be cached for a year without risking stale deploys.
_FINGERPRINTED_ASSET = re.compile(r"^assets/(?:[^/]+/)*[^/]+-[A-Za-z0-9_-]{8}\.(?:js|css|mjs|woff2|avif|webp|png|svg)$")
_COMPRESSIBLE_EXTENSIONS = frozenset({'.html', '.js', '.mjs', '.css', '.svg', '.json', '.xml', '.txt'})
_STATIC_EXTENSIONS = frozenset({'.js', '.mjs', '.css', '.avif', '.webp', '.png',
                               '.jpg', '.jpeg', '.gif', '.svg', '.ico', '.woff',
                               '.woff2', '.ttf', '.otf'})
_HTML_CACHE = 'no-cache, max-age=0, must-revalidate'
_PUBLIC_STATIC_CACHE = 'public, max-age=2592000, stale-while-revalidate=86400'
_FINGERPRINTED_CACHE = 'public, max-age=31536000, immutable'


REQUIRED_SCHEMA = '0042_openrouter_budget'
BILLING_SCHEMA = '0048_lava_billing'
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


def verify_release_schema(engine: Engine, *, check_billing_activation: bool = True) -> None:
    from kpdesk.billing.settings import BillingSettings

    try:
        # The isolated provisioner has no provider secrets. It checks structure;
        # the application startup/readiness always validates its actual switches.
        billing = BillingSettings.from_env() if check_billing_activation else None
    except (ValueError, TypeError):
        raise RuntimeError('Production billing settings are invalid.') from None
    with engine.begin() as connection:
        connection.execute(text('SET TRANSACTION READ ONLY'))
        available: object = connection.execute(text("SELECT to_regprocedure('public.kpdesk_runtime_schema_revision()') IS NOT NULL")).scalar_one()
        if available is not True:
            raise RuntimeError('Production database schema is not ready; runtime metadata function is missing.')
        result = connection.execute(SCHEMA_CHECK).one()
        if result[1] in ('0043_proposal_shares', '0044_universal_products', '0045_registry_retirement', '0046_provider_authority', '0047_auth_reset_serialization', BILLING_SCHEMA):
            from kpdesk.quotes.sharing import sharing_available

            if not sharing_available(connection) or connection.scalar(text('''SELECT relrowsecurity AND relforcerowsecurity
                FROM pg_catalog.pg_class WHERE oid=to_regclass('public.proposal_shares')''')) is not True:
                raise RuntimeError('Production database schema is not ready; proposal sharing gateway is incomplete.')
        if result[1] in ('0044_universal_products', '0045_registry_retirement', '0046_provider_authority', '0047_auth_reset_serialization', BILLING_SCHEMA):
            from kpdesk.db.universal_schema import universal_available

            if not universal_available(connection):
                raise RuntimeError('Production database schema is not ready; universal gateway is incomplete.')
        if result[1] in ('0045_registry_retirement', '0046_provider_authority', '0047_auth_reset_serialization', BILLING_SCHEMA):
            from kpdesk.db.supplier_registry_schema import registry_retired

            if not registry_retired(connection):
                raise RuntimeError('Production database schema is not ready; retired registry privileges remain.')
        if result[1] == BILLING_SCHEMA:
            from kpdesk.billing.schema import billing_available, billing_enabled

            if not billing_available(connection):
                raise RuntimeError('Production database schema is not ready; billing gateway is incomplete.')
            if billing is not None and billing_enabled(connection) is not billing.enabled:
                raise RuntimeError('Production billing application and database activation settings disagree.')
        elif billing is not None and billing.enabled:
            raise RuntimeError('Production billing schema is not ready; reviewed migration 0048 is required.')
    if (result[1] not in (REQUIRED_SCHEMA, '0043_proposal_shares', '0044_universal_products', '0045_registry_retirement', '0046_provider_authority', '0047_auth_reset_serialization', BILLING_SCHEMA)
            or (result[0], *result[2:]) != ('kpdesk_tenant_runtime', True, True, True)):
        raise RuntimeError('Production database schema is not ready; reviewed migrations must be applied separately.')


def _accepted_encoding_quality(header: str, coding: str) -> float:
    for item in header.split(','):
        name, *params = item.split(';')
        if name.strip().lower() != coding:
            continue
        quality = 1.0
        for parameter in params:
            key, separator, value = parameter.partition('=')
            if separator and key.strip().lower() == 'q':
                try:
                    quality = float(value.strip())
                except ValueError:
                    return 0.0
        return quality if 0.0 <= quality <= 1.0 else 0.0
    return 0.0


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

    @app.api_route('/{path:path}', methods=['GET', 'HEAD'], include_in_schema=False)
    def frontend(path: str, request: Request) -> FileResponse:
        if path == 'api' or path.startswith('api/'):
            raise HTTPException(404)
        normalized = path.rstrip('/')
        # Precompressed files are implementation details, never public URLs.
        if normalized.endswith(('.br', '.gz')):
            raise HTTPException(404)
        private = normalized in ('app', 'auth', 'proposal') or normalized.startswith(('app/', 'auth/', 'proposal/'))
        if not normalized:
            file = safe_file('index.html')
        elif normalized == 'proposal' or normalized.startswith('proposal/'):
            file = safe_file('proposal/index.html')
        elif private:
            file = safe_file('app/index.html' if normalized == 'app' or normalized.startswith('app/') else 'auth/login/index.html')
        else:
            file = safe_file(normalized) or safe_file(normalized+'/index.html')
        if file is None:
            error_page=safe_file('404.html')
            if error_page is None:
                raise HTTPException(404)
            return FileResponse(error_page, status_code=404, headers={
                'X-Robots-Tag': 'noindex, nofollow', 'Cache-Control': _HTML_CACHE,
            })
        if private:
            headers = {'Cache-Control': 'no-store', 'X-Robots-Tag': 'noindex, nofollow'}
        elif _FINGERPRINTED_ASSET.fullmatch(normalized):
            headers = {'Cache-Control': _FINGERPRINTED_CACHE}
        elif file.suffix.lower() in _STATIC_EXTENSIONS:
            # Non-fingerprinted public assets can change without their URL changing.
            headers = {'Cache-Control': _PUBLIC_STATIC_CACHE}
        elif normalized in ('robots.txt', 'sitemap.xml', 'site.webmanifest'):
            headers = {'Cache-Control': 'public, max-age=3600'}
        else:
            # In particular, never cache the version-specific HTML entrypoint.
            headers = {'Cache-Control': _HTML_CACHE}
        if file.suffix.lower() in _COMPRESSIBLE_EXTENSIONS:
            # Keep the private API out of compression; only public build
            # artifacts are eligible. The original URL stays unchanged.
            headers['Vary'] = 'Accept-Encoding'
            accepted = request.headers.get('accept-encoding', '')
            ordered = sorted(
                (('br', 'br'), ('gzip', 'gz')),
                key=lambda pair: _accepted_encoding_quality(accepted, pair[0]),
                reverse=True,
            )
            for coding, extension in ordered:
                if _accepted_encoding_quality(accepted, coding) <= 0:
                    continue
                compressed = safe_file(file.relative_to(root).as_posix() + '.' + extension)
                if compressed is not None:
                    headers['Content-Encoding'] = coding
                    media_type = guess_type(file.name)[0] or 'application/octet-stream'
                    return FileResponse(compressed, media_type=media_type, headers=headers)
        return FileResponse(file, headers=headers)
