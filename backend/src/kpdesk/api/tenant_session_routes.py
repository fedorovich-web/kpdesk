"""Explicitly opt-in secure session HTTP contract.

The default FastAPI app does NOT mount this router. The separate HTTPS-only
create_local_tenant_app uses dedicated auth/tenant PostgreSQL LOGINs and
restricted roles; public deployment remains blocked until verified onboarding
and TLS/proxy/browser validation. Default dependencies fail closed.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, File, Header, Query, Request, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from kpdesk.api.errors import ApiError
from kpdesk.catalog.parse import parse_price_list
from kpdesk.catalog.schema import ImportLimits
from kpdesk.catalog.storage import (
    CatalogImportConflict,
    InvalidCatalog,
    PublishedCatalog,
)
from kpdesk.catalog.tenant_storage import (
    CatalogSchemaIncompatible,
    TenantCatalogRepository,
)
from kpdesk.db.auth_sessions import (
    SESSION_LIFETIME,
    AuthSessionRepository,
    LoginDenied,
    SessionDenied,
    _token_digest,
    csrf_for_session,
)
from kpdesk.db.committed_login import CommittedLogin, CommittedSessionRevocation
from kpdesk.db.tenant_access import AuthenticatedUser, TenantAccessDenied

SESSION_COOKIE = "__Host-kpdesk_session"
COOKIE_AGE_SECONDS = int(SESSION_LIFETIME.total_seconds())


def get_unreleased_catalog_engine() -> Engine:
    """Dedicated least-privilege catalog Engine; unavailable in normal app."""
    raise ApiError(503, "auth_not_enabled", "Вход в аккаунт сейчас недоступен. Обратитесь к администратору KPDesk.")


def get_unreleased_auth_repository(
    engine: Annotated[Engine, Depends(get_unreleased_catalog_engine)],
) -> Iterator[AuthSessionRepository]:
    """One DB transaction per request, committed before sending HTTP bytes.

    All routes require Depends(..., scope='function'). An endpoint exception
    propagates through this context and ROLLBACKs; successful reads and writes
    COMMIT before the response is sent. No global mutable shared connection.
    """
    with engine.begin() as connection:
        yield AuthSessionRepository(connection)


def get_unreleased_login_engine() -> Engine:
    """Dedicated auth-service Engine. Never share catalog DB login credentials."""
    raise ApiError(503, "auth_not_enabled", "Вход в аккаунт сейчас недоступен. Обратитесь к администратору KPDesk.")


unreleased_router = APIRouter(prefix="/api/session")


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class LoginResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    csrf_token: str
    expires_at: datetime


class WhoAmIResponse(BaseModel):
    user_id: uuid.UUID


class ResumeSessionResponse(BaseModel):
    user_id: uuid.UUID
    csrf_token: str


class OrganizationSummary(BaseModel):
    id: uuid.UUID
    name: str
    role: str


class MyOrganizationsResponse(BaseModel):
    organizations: list[OrganizationSummary]


class CurrentTenantCatalogResponse(BaseModel):
    version_id: uuid.UUID
    source_sha256: str
    rows_count: int


class TenantCatalogHistory(BaseModel):
    items: list[CurrentTenantCatalogResponse]
    next_offset: int | None


class CatalogValidationIssue(BaseModel):
    code: str
    message: str
    row: int | None
    field: str | None


class CatalogPreflightResponse(BaseModel):
    source_sha256: str
    publishable: bool
    rows_count: int
    errors: list[CatalogValidationIssue]
    total_errors: int


def _required_session(
    repository: AuthSessionRepository,
    session_cookie: str | None,
    *,
    csrf: str | None = None,
    write: bool = False,
) -> AuthenticatedUser:
    try:
        return repository.authenticate(
            session_cookie or "",
            operation="write" if write else "read",
            csrf=csrf,
        )
    except SessionDenied as error:
        raise ApiError(401, "invalid_session", "Для продолжения войдите в KPDesk.") from error


@unreleased_router.post("/login", response_model=LoginResponse)
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    auth_engine: Annotated[Engine, Depends(get_unreleased_login_engine)],
) -> LoginResponse:
    try:
        issued = CommittedLogin(auth_engine).attempt(
            body.email, body.password,
            peer=request.client.host if request.client else "",
        )
    except LoginDenied as error:
        raise ApiError(401, "invalid_login", "Неверная почта или пароль.") from error
    response.set_cookie(
        key=SESSION_COOKIE,
        value=issued.session_token,
        max_age=COOKIE_AGE_SECONDS,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    response.headers["Cache-Control"] = "no-store"
    return LoginResponse(csrf_token=issued.csrf_token, expires_at=issued.expires_at)


@unreleased_router.get("/me", response_model=WhoAmIResponse)
def who_am_i(
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> WhoAmIResponse:
    person = _required_session(repository, session_cookie)
    return WhoAmIResponse(user_id=person.user_id)


@unreleased_router.get("/resume", response_model=ResumeSessionResponse)
def resume_session(
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> ResumeSessionResponse:
    identity = _required_session(repository, session_cookie)
    # The cookie stays HttpOnly. Only after the DB validates the live session
    # do we recover a token bound to its original CSRF hash.
    candidate = csrf_for_session(session_cookie or "")
    _required_session(repository, session_cookie, csrf=candidate, write=True)
    response.headers["Cache-Control"] = "no-store"
    return ResumeSessionResponse(user_id=identity.user_id, csrf_token=candidate)


@unreleased_router.get("/organizations", response_model=MyOrganizationsResponse)
def my_organizations(
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> MyOrganizationsResponse:
    _required_session(repository, session_cookie)
    digest = _token_digest(session_cookie or "")
    rows = repository.connection.execute(text("""
        SELECT organization_id,organization_name,membership_role
        FROM public.kpdesk_user_organizations(:digest)
    """), {"digest": digest}).all()
    return MyOrganizationsResponse(
        organizations=[
            OrganizationSummary(
                id=row.organization_id, name=row.organization_name,
                role=row.membership_role,
            )
            for row in rows
        ],
    )


@unreleased_router.post("/logout", status_code=204)
def logout(
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    auth_engine: Annotated[Engine, Depends(get_unreleased_login_engine)],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> Response:
    _required_session(repository, session_cookie, csrf=csrf, write=True)
    if session_cookie is not None:
        CommittedSessionRevocation(auth_engine).revoke(session_cookie)
    response.delete_cookie(
        SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict",
    )
    response.headers["Cache-Control"] = "no-store"
    response.status_code = 204
    return response


@unreleased_router.post(
    "/organizations/{organization_id}/catalog/preflight",
    response_model=CatalogPreflightResponse,
)
def preflight_tenant_catalog(
    organization_id: uuid.UUID,
    request: Request,
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    upload: Annotated[UploadFile, File(alias="file")],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> CatalogPreflightResponse:
    identity = _required_session(repository, session_cookie, csrf=csrf, write=True)
    store = TenantCatalogRepository(
        repository.connection, identity, organization_id,
        session_token=session_cookie, csrf_token=csrf,
    )
    try:
        store._authorize("write")
    except TenantAccessDenied as error:
        raise ApiError(403, "organization_forbidden", "У вас нет доступа к данным этой организации.") from error
    filename = upload.filename or ""
    if (not filename or len(filename) > 250 or "/" in filename
        or "\\" in filename or any(ord(c) < 32 for c in filename)):
        raise ApiError(422, "invalid_filename", "Имя файла прайса недопустимо. Переименуйте файл и выберите его заново.")
    maximum = request.app.state.settings.max_upload_bytes
    content = upload.file.read(maximum + 1)
    if len(content) > maximum:
        raise ApiError(413, "catalog_too_large", "Размер файла прайса превышает лимит загрузки.")
    source = parse_price_list(filename, content, ImportLimits(max_upload_bytes=maximum))
    issues = [CatalogValidationIssue(
        code=issue.code, message=issue.message, row=issue.row, field=issue.field,
    ) for issue in source.errors[:30]]
    unavailable: tuple[str, ...] = ()
    if source.publishable:
        try:
            unavailable = store.unavailable_categories(source.products)
        except (RuntimeError, SQLAlchemyError) as error:
            raise ApiError(
                503, "catalog_schema_unavailable",
                "Не удалось проверить возможность загрузки прайса. Попробуйте позже.",
            ) from error
        if unavailable:
            issues.append(CatalogValidationIssue(
                code="database_category_unavailable",
                message="Обратитесь к администратору KPDesk: недоступны категории прайса "
                        + ", ".join(unavailable),
                row=None, field="category",
            ))
    response.headers["Cache-Control"] = "no-store"
    return CatalogPreflightResponse(
        source_sha256=source.source_sha256,
        publishable=source.publishable and not unavailable,
        rows_count=len(source.products),
        errors=issues,
        total_errors=len(source.errors) + int(bool(unavailable)),
    )


@unreleased_router.post(
    "/organizations/{organization_id}/catalog/import",
    response_model=CurrentTenantCatalogResponse,
    status_code=201,
)
def import_tenant_catalog(
    organization_id: uuid.UUID,
    request: Request,
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    upload: Annotated[UploadFile, File(alias="file")],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    expected_source_sha256: Annotated[str | None, Header(alias="X-Source-SHA256")] = None,
) -> CurrentTenantCatalogResponse:
    identity = _required_session(repository, session_cookie, csrf=csrf, write=True)
    if (
        idempotency_key is None or not 1 <= len(idempotency_key) <= 128
        or any(not 33 <= ord(c) <= 126 for c in idempotency_key)
    ):
        raise ApiError(422, "invalid_idempotency_key", "Не удалось определить запрос загрузки. Проверьте активный прайс перед повторной попыткой.")
    filename = upload.filename or ""
    if (
        not filename or len(filename) > 250
        or "/" in filename or "\\" in filename
        or any(ord(c) < 32 for c in filename)
    ):
        raise ApiError(422, "invalid_filename", "Имя файла прайса недопустимо. Переименуйте файл и выберите его заново.")
    # Authorization is checked before reading the body into application memory.
    store = TenantCatalogRepository(
        repository.connection, identity, organization_id,
        session_token=session_cookie, csrf_token=csrf,
    )
    try:
        store._authorize("write")
    except TenantAccessDenied as error:
        raise ApiError(403, "organization_forbidden", "У вас нет доступа к данным этой организации.") from error
    maximum = request.app.state.settings.max_upload_bytes
    content = upload.file.read(maximum + 1)
    if len(content) > maximum:
        raise ApiError(413, "catalog_too_large", "Размер файла прайса превышает лимит загрузки.")
    source = parse_price_list(filename, content, ImportLimits(max_upload_bytes=maximum))
    if expected_source_sha256 is not None and expected_source_sha256 != source.source_sha256:
        raise ApiError(409, "source_changed", "Файл изменился после проверки. Проверьте выбранный файл заново перед загрузкой.")
    if not source.publishable:
        raise ApiError(422, "catalog_validation_failed", "Прайс не прошёл проверку. Проверьте данные файла перед загрузкой.")
    try:
        created = store.publish(source, idempotency_key, source.source_sha256)
    except TenantAccessDenied as error:
        raise ApiError(403, "organization_forbidden", "У вас нет доступа к данным этой организации.") from error
    except CatalogImportConflict as error:
        raise ApiError(409, "catalog_import_conflict", "Не удалось подтвердить повторную загрузку прайса. Проверьте активную версию перед новой попыткой.") from error
    except CatalogSchemaIncompatible as error:
        raise ApiError(
            409, "catalog_schema_taxonomy_mismatch",
            "Обратитесь к администратору KPDesk: недоступны категории прайса " + ", ".join(error.categories),
        ) from error
    except (RuntimeError, SQLAlchemyError) as error:
        raise ApiError(503, "catalog_schema_unavailable", "Не удалось подтвердить загрузку прайса. Проверьте активный прайс перед повторной попыткой.") from error
    except InvalidCatalog as error:
        raise ApiError(422, "catalog_validation_failed", "Прайс не прошёл проверку. Проверьте данные файла перед загрузкой.") from error
    response.headers["Cache-Control"] = "no-store"
    return CurrentTenantCatalogResponse(
        version_id=created.id,
        source_sha256=created.source_sha256,
        rows_count=created.rows_count,
    )


@unreleased_router.get(
    "/organizations/{organization_id}/catalog/current",
    response_model=CurrentTenantCatalogResponse,
)
def current_tenant_catalog(
    organization_id: uuid.UUID,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> CurrentTenantCatalogResponse:
    identity = _required_session(repository, session_cookie)
    try:
        catalog: PublishedCatalog | None = TenantCatalogRepository(
            repository.connection, identity, organization_id,
            session_token=session_cookie,
        ).current()
    except TenantAccessDenied as error:
        raise ApiError(403, "organization_forbidden", "У вас нет доступа к данным этой организации.") from error
    if catalog is None:
        raise ApiError(404, "no_active_catalog", "Активный прайс пока не загружен.")
    return CurrentTenantCatalogResponse(
        version_id=catalog.id,
        source_sha256=catalog.source_sha256,
        rows_count=catalog.rows_count,
    )

@unreleased_router.get(
    "/organizations/{organization_id}/catalog/versions",
    response_model=TenantCatalogHistory,
)
def list_tenant_catalog_versions(
    organization_id: uuid.UUID,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    response: Response,
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
) -> TenantCatalogHistory:
    identity = _required_session(repository, session_cookie)
    store = TenantCatalogRepository(
        repository.connection, identity, organization_id,
        session_token=session_cookie,
    )
    try:
        versions = store.versions(limit=100, offset=offset)
    except TenantAccessDenied as error:
        raise ApiError(403, "organization_forbidden", "У вас нет доступа к данным этой организации.") from error
    response.headers["Cache-Control"] = "no-store"
    return TenantCatalogHistory(
        items=[
            CurrentTenantCatalogResponse(
                version_id=v.id, source_sha256=v.source_sha256, rows_count=v.rows_count,
            ) for v in versions
        ],
        next_offset=offset + len(versions) if len(versions) == 100 else None,
    )
