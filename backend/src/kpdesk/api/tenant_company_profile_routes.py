from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, File, Header, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy.exc import SQLAlchemyError

from kpdesk.api.errors import ApiError, company_logo_error_message
from kpdesk.api.tenant_session_routes import (
    SESSION_COOKIE,
    _required_session,
    get_unreleased_auth_repository,
)
from kpdesk.company.logo import MAX_UPLOAD_BYTES, CompanyLogo, prepare_logo
from kpdesk.company.schema import CompanyProfileData, CompanyProfileSnapshot
from kpdesk.company.storage import CompanyProfileConflict, CompanyProfileStore
from kpdesk.db.auth_sessions import AuthSessionRepository
from kpdesk.db.authenticated_tenant import bind_authenticated_tenant
from kpdesk.db.tenant_access import TenantAccessDenied

router = APIRouter(prefix="/api/session/organizations/{organization_id}/company-profile")


class ProfileResponse(BaseModel):
    profile: CompanyProfileSnapshot | None


class SaveProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data: CompanyProfileData
    expected_revision: int = Field(ge=0, le=2147483646, strict=True)
    reviewed: StrictBool


class LogoResponse(BaseModel):
    logo: CompanyLogo


@router.post("/logo/prepare", response_model=LogoResponse)
def prepare_profile_logo(
    organization_id: UUID, response: Response,
    upload: Annotated[UploadFile, File(alias="file")],
    repository: Annotated[AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function")],
    cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> LogoResponse:
    _required_session(repository, cookie, write=True, csrf=csrf)
    try:
        bind_authenticated_tenant(repository.connection, session_token=cookie or "",
                                  organization_id=organization_id, operation="manage", csrf=csrf)
    except TenantAccessDenied as error:
        raise ApiError(403, "organization_forbidden", "Логотип компании изменяет руководитель.") from error
    except (SQLAlchemyError, RuntimeError) as error:
        raise ApiError(503, "company_logo_unavailable", "Не удалось проверить доступ к компании.") from error
    raw = upload.file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ApiError(413, "company_logo_too_large", "Логотип должен быть не больше 1 МиБ.")
    try:
        logo = prepare_logo(raw)
    except ValueError as error:
        raise ApiError(422, "company_logo_invalid", company_logo_error_message(str(error))) from error
    response.headers["Cache-Control"] = "no-store"
    return LogoResponse(logo=logo)


@router.get("", response_model=ProfileResponse)
def read_profile(
    organization_id: UUID, response: Response,
    repository: Annotated[AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function")],
    cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> ProfileResponse:
    _required_session(repository, cookie, write=False)
    try:
        profile = CompanyProfileStore(repository.connection, organization_id, cookie or "").get()
    except TenantAccessDenied as error:
        raise ApiError(403, "organization_forbidden", "У вас нет доступа к сведениям этой компании.") from error
    except (SQLAlchemyError, RuntimeError, TypeError, ValueError) as error:
        raise ApiError(503, "company_profile_unavailable", "Не удалось загрузить настройки компании.") from error
    response.headers["Cache-Control"] = "no-store"
    return ProfileResponse(profile=profile)


@router.put("", response_model=ProfileResponse)
def save_profile(
    organization_id: UUID, body: SaveProfile, response: Response,
    repository: Annotated[AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function")],
    cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> ProfileResponse:
    _required_session(repository, cookie, write=True, csrf=csrf)
    if not body.reviewed:
        raise ApiError(422, "profile_review_required", "Подтвердите достоверность сведений компании.")
    try:
        profile = CompanyProfileStore(repository.connection, organization_id, cookie or "", csrf).save(
            body.data, body.expected_revision)
    except CompanyProfileConflict as error:
        raise ApiError(409, "stale_company_profile", "Сведения компании уже изменены. Загрузите сохранённую версию и проверьте правки перед сохранением.") from error
    except TenantAccessDenied as error:
        raise ApiError(403, "organization_forbidden", "Настройки компании изменяет руководитель.") from error
    except (SQLAlchemyError, RuntimeError, TypeError, ValueError) as error:
        raise ApiError(503, "company_profile_unavailable", "Не удалось подтвердить сохранение сведений компании. Обновите настройки и проверьте результат перед повторной попыткой.") from error
    response.headers["Cache-Control"] = "no-store"
    return ProfileResponse(profile=profile)
