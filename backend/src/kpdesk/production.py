"""Fail-closed ASGI factory for a public, HTTPS-terminated tenant-only KPDesk.

Run explicitly with `uvicorn kpdesk.production:create_production_app --factory`.
Do not expose `kpdesk.main:app`, which is the legacy local API.
"""
from __future__ import annotations

import os
from collections.abc import Mapping

from fastapi import FastAPI

from kpdesk.config import AppSettings
from kpdesk.main import create_app
from kpdesk.public_origin import public_hostname


def public_tenant_settings(env: Mapping[str, str] | None = None) -> AppSettings:
    """Derive exact Host/Origin allowlists; never infer a production domain."""
    variables = os.environ if env is None else env
    raw = variables.get("KP_DESK_PUBLIC_ORIGIN", "")
    hostname = public_hostname(raw)
    return AppSettings(
        allowed_hosts=(hostname,),
        allowed_tenant_origins=(raw,),
        allowed_origins=(),
        enable_synthetic_ml=False,
    )


def create_production_app() -> FastAPI:
    """Mount only session-scoped routes after strict origin and database preflight."""
    settings = public_tenant_settings()
    # Production must never fall back to a developer's localhost DB or credential file.
    for variable in (
        "KP_DESK_DB_HOST", "KP_DESK_DB_PORT", "KP_DESK_SERVICE_CREDENTIALS_FILE",
    ):
        if not os.environ.get(variable):
            raise RuntimeError(f"Production database setting missing: {variable}")
    from kpdesk.db.live_service_engines import live_service_engines
    from kpdesk.release_runtime import mount_built_frontend, verify_release_schema

    _, tenant_engine = live_service_engines()
    verify_release_schema(tenant_engine)
    app = create_app(settings=settings, tenant_sessions=True)
    app.state.runtime_readiness = lambda: verify_release_schema(tenant_engine)
    if 'KP_DESK_FRONTEND_DIR' in os.environ:
        mount_built_frontend(app, os.environ['KP_DESK_FRONTEND_DIR'])
    return app
