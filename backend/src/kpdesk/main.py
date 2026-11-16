"""Separate legacy-local and session-scoped tenant ASGI applications."""

from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import RequestResponseEndpoint

from kpdesk.api.body_limit import RequestBodyLimitMiddleware
from kpdesk.api.errors import ApiError, json_error
from kpdesk.api.integration_routes import router as integration_router
from kpdesk.api.persisted_quote_routes import router as stored_quote_router
from kpdesk.api.quote_routes import router as quote_router
from kpdesk.api.routes import router
from kpdesk.api.supplier_routes import router as supplier_router
from kpdesk.config import AppSettings


def create_app(
    *, testing: bool = False, settings: AppSettings | None = None,
    tenant_sessions: bool = False,
) -> FastAPI:
    # Test factories must not inherit an operator's opt-in local ML setting.
    # Tests that exercise ML pass AppSettings(enable_synthetic_ml=True) explicitly.
    if settings is not None:
        config = settings
    elif testing:
        config = AppSettings(enable_synthetic_ml=False)
    else:
        config = AppSettings()
    hosts = set(config.allowed_hosts)
    if testing:
        hosts.add("testserver")

    app = FastAPI(
        title="KPDesk Tenant API" if tenant_sessions else "KPDesk Local API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = config
    app.state.testing = testing
    permitted_origins = (
        config.allowed_tenant_origins if tenant_sessions else config.allowed_origins
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(permitted_origins),
        allow_credentials=tenant_sessions,
        expose_headers=(
            ["X-Catalog-SHA256", "X-Source-SHA256", "X-Excluded-Rows"]
            if tenant_sessions else []
        ),
        allow_methods=(
            ["GET", "POST", "PATCH", "DELETE"]
            if tenant_sessions else ["GET", "POST"]
        ),
        allow_headers=(
            ["Content-Type", "Idempotency-Key", "X-CSRF-Token", "X-Source-SHA256",
             "X-Catalog-SHA256", "X-Proposal-Token"]
            if tenant_sessions else
            ["Content-Type", "Idempotency-Key", "X-KPDesk-Token"]
        ),
    )
    app.add_middleware(RequestBodyLimitMiddleware, max_upload_bytes=config.max_upload_bytes)

    @app.middleware("http")
    async def local_boundary(request: Request, call_next: RequestResponseEndpoint) -> Response:
        request.state.request_id = uuid4().hex
        host = request.headers.get("host", "").split(":", maxsplit=1)[0].lower()
        if host not in hosts:
            return json_error(request, 400, "invalid_host", "Недопустимый Host.")
        if tenant_sessions and request.url.scheme != "https":
            return json_error(request, 403, "https_required", "Требуется HTTPS.")
        origin = request.headers.get("origin")
        if origin is not None and origin not in permitted_origins:
            return json_error(request, 403, "invalid_origin", "Недопустимый Origin.")
        webhook = (tenant_sessions and request.method == 'POST'
                   and request.url.path == '/api/billing/lava/webhook')
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and origin is None and not webhook:
            return json_error(request, 403, "missing_origin", "Требуется заголовок Origin.")
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        if request.url.path.endswith(("/company-lookup", "/document-preview")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.middleware("http")
    async def response_privacy(request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        if tenant_sessions:
            response.headers.update({
                'X-Content-Type-Options': 'nosniff',
                'Referrer-Policy': 'no-referrer',
                'X-Frame-Options': 'DENY',
                'Content-Security-Policy': (
                    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                    "img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; "
                    # The document preview/public share render server PDF bytes via blob URLs.
                    "frame-src blob: 'self'; object-src blob:; "
                    "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
                ),
            })
            if request.url.scheme == 'https':
                response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        parts = request.url.path.split('/')
        if (request.url.path.startswith('/api/session/yandex/')
                or request.url.path == '/api/session/providers'):
            response.headers.update({'Cache-Control': 'no-store', 'X-Robots-Tag': 'noindex, nofollow'})
        if request.url.path.startswith('/api/billing/') or (
            request.url.path.startswith('/api/session/organizations/') and 'billing' in parts
        ):
            response.headers.update({'Cache-Control': 'no-store', 'X-Robots-Tag': 'noindex, nofollow'})
        if (len(parts) >= 6 and parts[1:4] == ['api', 'session', 'organizations']
                and parts[5] == 'universal'):
            # Dependencies do not carry headers into exception responses or
            # explicit Response objects (including feedback withdrawal's 204).
            response.headers['Cache-Control'] = 'no-store'
        if (request.url.path.rstrip('/') == '/proposal'
                or request.url.path.startswith('/api/public/proposal/')
                or '/shares' in request.url.path):
            from kpdesk.api.proposal_share_routes import PRIVATE_HEADERS

            response.headers.update(PRIVATE_HEADERS)
        return response

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exception: ApiError) -> JSONResponse:
        from kpdesk.billing.errors import billing_error

        exception = billing_error(exception) or exception
        return json_error(
            request, exception.status_code, exception.code, exception.message,
            field=exception.field, row=exception.row,
            retry_after_seconds=exception.retry_after_seconds,
        )

    from sqlalchemy.exc import SQLAlchemyError

    @app.exception_handler(SQLAlchemyError)
    async def sql_error(request: Request, exception: SQLAlchemyError) -> JSONResponse:
        from kpdesk.billing.errors import billing_error

        error = billing_error(exception) or ApiError(
            503, 'database_unavailable', 'Данные временно недоступны. Повторите действие позже.')
        return json_error(request, error.status_code, error.code, error.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exception: RequestValidationError) -> JSONResponse:
        return json_error(
            request, 422, "validation_error", "Некорректные данные запроса."
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exception: StarletteHTTPException) -> JSONResponse:
        if getattr(request.state, "body_limit_exceeded", False):
            return json_error(request, 413, "file_too_large", "Файл превышает лимит.")
        return json_error(
            request, exception.status_code, "http_error", "Маршрут или запрос недоступен."
        )

    if tenant_sessions:
        # Opt-in local ASGI factory only. The normal app global does not mount
        # cookie sessions, and the owner-token/global write routes are absent.
        from kpdesk.api.account_routes import router as account_router
        from kpdesk.api.billing_routes import router as billing_router
        from kpdesk.api.member_routes import router as member_router
        from kpdesk.api.proposal_share_routes import router as proposal_share_router
        from kpdesk.api.tenant_candidate_adjudication_routes import (
            router as tenant_candidate_adjudication_router,
        )
        from kpdesk.api.tenant_candidate_review_routes import (
            router as tenant_candidate_review_router,
        )
        from kpdesk.api.tenant_company_lookup_routes import router as tenant_company_lookup_router
        from kpdesk.api.tenant_company_profile_routes import router as tenant_company_profile_router
        from kpdesk.api.tenant_document_routes import router as tenant_document_router
        from kpdesk.api.tenant_extraction_routes import router as tenant_extract_router
        from kpdesk.api.tenant_matching_routes import router as tenant_matching_router
        from kpdesk.api.tenant_quote_routes import unreleased_tenant_quote_router
        from kpdesk.api.tenant_session_routes import (
            get_unreleased_catalog_engine,
            get_unreleased_login_engine,
            unreleased_router,
        )
        from kpdesk.api.tenant_supplier_canonical_routes import (
            router as tenant_supplier_canonical_router,
        )
        from kpdesk.api.tenant_supplier_comparison_routes import (
            router as tenant_supplier_comparison_router,
        )
        from kpdesk.api.tenant_supplier_historical_routes import (
            router as tenant_supplier_historical_router,
        )
        from kpdesk.api.tenant_supplier_precision_routes import (
            router as tenant_supplier_precision_router,
        )
        from kpdesk.api.tenant_supplier_profile_routes import (
            router as tenant_supplier_profile_router,
        )
        from kpdesk.api.tenant_supplier_unit_routes import (
            router as tenant_supplier_unit_router,
        )
        from kpdesk.api.universal_routes import router as universal_router
        from kpdesk.api.yandex_routes import router as yandex_router
        from kpdesk.db.live_service_engines import live_service_engines

        auth_engine, catalog_engine = live_service_engines()
        app.dependency_overrides[get_unreleased_catalog_engine] = lambda: catalog_engine
        app.dependency_overrides[get_unreleased_login_engine] = lambda: auth_engine

        @app.get("/api/health/live")
        def tenant_health_live() -> dict[str, str]:
            return {"status": "ok"}

        @app.get("/api/health/ready")
        def tenant_health_ready() -> dict[str, str]:
            release_readiness = getattr(app.state, 'runtime_readiness', None)
            if release_readiness is not None:
                from sqlalchemy.exc import SQLAlchemyError

                try:
                    release_readiness()
                except (RuntimeError, SQLAlchemyError) as error:
                    raise ApiError(503, 'tenant_schema_unavailable', 'Tenant schema unavailable.') from error
            with catalog_engine.begin() as connection:
                if connection.scalar(text("SELECT current_user")) != (
                    "kpdesk_tenant_runtime"
                ):
                    raise ApiError(503, "tenant_store_unavailable", "Tenant DB unavailable.")
            return {"status": "ready"}

        app.include_router(unreleased_router)
        app.include_router(account_router)
        app.include_router(billing_router)
        app.include_router(yandex_router)
        app.include_router(member_router)
        app.include_router(tenant_extract_router)
        app.include_router(tenant_matching_router)
        app.include_router(universal_router)
        app.include_router(unreleased_tenant_quote_router)
        app.include_router(tenant_document_router)
        app.include_router(proposal_share_router)
        app.include_router(tenant_company_profile_router)
        app.include_router(tenant_company_lookup_router)
        app.include_router(tenant_supplier_comparison_router)
        app.include_router(tenant_supplier_historical_router)
        app.include_router(tenant_supplier_precision_router)
        app.include_router(tenant_supplier_canonical_router)
        if testing:
            # A73 checkpoint contract is restricted to disposable clone E2E;
            # the production app has no checkpoint routes or migrated tables.
            from kpdesk.api.tenant_supplier_checkpoint_routes import (
                router as tenant_supplier_checkpoint_router,
            )
            app.include_router(tenant_supplier_checkpoint_router)
            from kpdesk.api.tenant_supplier_checkpoint_page_routes import (
                router as tenant_supplier_checkpoint_page_router,
            )
            app.include_router(tenant_supplier_checkpoint_page_router)
            from kpdesk.api.tenant_supplier_publication_routes import (
                router as tenant_supplier_publication_router,
            )
            app.include_router(tenant_supplier_publication_router)
        app.include_router(tenant_supplier_profile_router)
        app.include_router(tenant_supplier_unit_router)
        app.include_router(tenant_candidate_review_router)
        app.include_router(tenant_candidate_adjudication_router)
    else:
        app.include_router(router)
        app.include_router(supplier_router)
        app.include_router(integration_router)
        app.include_router(quote_router)
        app.include_router(stored_quote_router)
    return app



def create_local_tenant_app() -> FastAPI:
    """Explicit opt-in session API.

    Requires HTTPS with a trusted certificate; HTTP is rejected. The normal
    `app` object below remains the legacy local API and does not gain tenant
    routes merely because PostgreSQL has auth tables.
    """
    return create_app(tenant_sessions=True)


app = create_app()
