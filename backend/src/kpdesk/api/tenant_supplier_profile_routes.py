"""Source-bound, authenticated supplier mapping approvals and exact-schema lookup."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    File,
    Form,
    Header,
    Query,
    Request,
    Response,
    UploadFile,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from kpdesk.api.errors import ApiError
from kpdesk.api.supplier_routes import MappingBody, _safe_uploaded_source
from kpdesk.api.tenant_session_routes import (
    SESSION_COOKIE,
    _required_session,
    get_unreleased_auth_repository,
)
from kpdesk.api.tenant_supplier_precision_routes import _authorized_supplier_write
from kpdesk.catalog.supplier_inference import SourceInspection, inspect_supplier_source
from kpdesk.catalog.supplier_mapping_storage import TenantSupplierMappingStore
from kpdesk.catalog.supplier_profiles import approve_supplier_profile, reuse_supplier_profile
from kpdesk.db.auth_sessions import AuthSessionRepository
from kpdesk.db.tenant_access import TenantAccessDenied

router = APIRouter(
    prefix="/api/session/organizations/{organization_id}/suppliers/{supplier_id}/mapping-profiles",
)


class MappingApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mapping: MappingBody
    price_basis: Literal["standard", "retail", "wholesale", "rrp", "vat_included"]
    evidence: str = Field(min_length=8, max_length=500)
    explicitly_confirmed: Literal[True]
    confirmed_currency: Literal["RUB"] | None = None


class MappingProfileResponse(BaseModel):
    found: bool
    source_sha256: str
    profile_sha256: str | None = None
    fingerprint: str | None = None
    approved_by: uuid.UUID | None = None
    price_basis: str | None = None
    columns: dict[str, int] | None = None
    header_row: int | None = None
    sheet: str | None = None
    reviewed: bool = False
    canonical_importable: bool = False
    model_training_authorized: bool = False


class MappingProfileHistoryItem(BaseModel):
    profile_sha256: str
    fingerprint: str
    source_format: str
    columns: dict[str, int | str]
    header_row: int | None
    sheet: str | None
    delimiter: str | None
    encoding: str | None
    items_key: str | None
    confirmed_currency: Literal["RUB"] | None
    price_basis: str
    approved_by: uuid.UUID
    approved_source_sha256: str
    review_evidence: str
    reviewed_at: datetime


class MappingProfileHistoryResponse(BaseModel):
    items: list[MappingProfileHistoryItem]
    next_offset: int | None
    canonical_importable: bool = False
    model_training_authorized: bool = False


def _source(request: Request, upload: UploadFile, sheet: str | None) -> tuple[str, bytes, SourceInspection]:
    filename, payload = _safe_uploaded_source(request, upload)
    if not filename.casefold().endswith((".csv", ".xls", ".xlsx")):
        raise ApiError(422, "invalid_supplier_source", "Этот формат файла недоступен на данном шаге. Выберите прайс поддерживаемого формата.")
    try:
        inspection = inspect_supplier_source(filename, payload, sheet=sheet)
    except (ValueError, TypeError) as error:
        raise ApiError(422, "source_inspection_required", "Не удалось определить структуру прайса. Проверьте файл и сопоставьте колонки вручную.") from error
    return filename, payload, inspection


def _store(
    repository: AuthSessionRepository, session_cookie: str | None,
    organization_id: uuid.UUID, supplier_id: str, csrf: str | None,
) -> TenantSupplierMappingStore:
    scope = _authorized_supplier_write(
        repository, session_cookie, organization_id, supplier_id, csrf,
    )
    return TenantSupplierMappingStore(
        repository.connection, scope.identity, organization_id,
        session_cookie or "", csrf or "",
    )


def _result(profile: object, source_sha256: str) -> MappingProfileResponse:
    from kpdesk.catalog.supplier_profiles import ReviewedSupplierProfile

    if not isinstance(profile, ReviewedSupplierProfile):
        return MappingProfileResponse(found=False, source_sha256=source_sha256)
    return MappingProfileResponse(
        found=True, source_sha256=source_sha256,
        profile_sha256=profile.profile_sha256,
        fingerprint=profile.fingerprint,
        approved_by=profile.approved_by,
        price_basis=profile.price_basis,
        columns={key: int(value) for key, value in profile.columns},
        header_row=profile.header_row, sheet=profile.sheet,
        reviewed=True,
    )


@router.get("/history", response_model=MappingProfileHistoryResponse)
def supplier_mapping_history(
    organization_id: uuid.UUID, supplier_id: str, response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    limit: Annotated[int, Query(ge=1, le=49)] = 20,
    offset: Annotated[int, Query(ge=0, le=500)] = 0,
) -> MappingProfileHistoryResponse:
    identity = _required_session(repository, session_cookie)
    store = TenantSupplierMappingStore(
        repository.connection, identity, organization_id, session_cookie or "",
    )
    try:
        records = store.history(supplier_id, limit=limit + 1, offset=offset)
    except ValueError as error:
        raise ApiError(422, "invalid_mapping_history", "Не удалось загрузить историю настроек прайса. Проверьте выбранного поставщика и параметры списка.") from error
    except TenantAccessDenied as error:
        raise ApiError(403, "organization_forbidden", "У вас нет доступа к настройкам прайса этого поставщика.") from error
    except (SQLAlchemyError, RuntimeError) as error:
        raise ApiError(503, "mapping_store_unavailable", "Не удалось получить или сохранить проверку прайса. Проверьте сохранённые решения перед повторной попыткой.") from error
    items = [
        MappingProfileHistoryItem(
            profile_sha256=entry.profile.profile_sha256,
            fingerprint=entry.profile.fingerprint,
            source_format=entry.profile.source_format,
            columns=dict(entry.profile.columns),
            header_row=entry.profile.header_row,
            sheet=entry.profile.sheet,
            delimiter=entry.profile.delimiter,
            encoding=entry.profile.encoding,
            items_key=entry.profile.items_key,
            confirmed_currency=entry.profile.confirmed_currency,
            price_basis=entry.profile.price_basis,
            approved_by=entry.profile.approved_by,
            approved_source_sha256=entry.profile.approved_source_sha256,
            review_evidence=entry.profile.review_evidence,
            reviewed_at=entry.reviewed_at,
        )
        for entry in records[:limit]
    ]
    response.headers["Cache-Control"] = "no-store"
    return MappingProfileHistoryResponse(
        items=items,
        next_offset=offset + limit if len(records) > limit else None,
    )


@router.post("/match", response_model=MappingProfileResponse)
def match_supplier_mapping(
    organization_id: uuid.UUID, supplier_id: str, request: Request,
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    upload: Annotated[UploadFile, File(alias="file")],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    sheet: Annotated[str | None, Form(max_length=100)] = None,
) -> MappingProfileResponse:
    store = _store(repository, session_cookie, organization_id, supplier_id, csrf)
    filename, content, inspection = _source(request, upload, sheet)
    try:
        profile = store.latest(supplier_id, inspection)
        if profile is not None:
            reuse_supplier_profile(
                profile, inspection, filename, content,
                organization_id=organization_id, supplier_id=supplier_id,
            )
    except (ValueError, TenantAccessDenied) as error:
        raise ApiError(409, "supplier_profile_drift", "Не удалось применить сохранённые настройки прайса. Проверьте доступ и актуальность файла, затем заново проверьте настройки колонок.") from error
    except (RuntimeError, SQLAlchemyError) as error:
        raise ApiError(503, "mapping_store_unavailable", "Не удалось получить или сохранить проверку прайса. Проверьте сохранённые решения перед повторной попыткой.") from error
    response.headers["Cache-Control"] = "no-store"
    return _result(profile, inspection.source_sha256)


@router.post("", response_model=MappingProfileResponse, status_code=201)
def approve_supplier_mapping(
    organization_id: uuid.UUID, supplier_id: str, request: Request,
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    upload: Annotated[UploadFile, File(alias="file")],
    approval_json: Annotated[str, Form()],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> MappingProfileResponse:
    store = _store(repository, session_cookie, organization_id, supplier_id, csrf)
    if len(approval_json) > 8192:
        raise ApiError(422, "invalid_mapping_approval", "Данные подтверждения колонок превышают допустимый объём. Проверьте настройки и сократите пояснение.")
    try:
        body = MappingApproval.model_validate_json(approval_json)
    except ValidationError as error:
        raise ApiError(422, "invalid_mapping_approval", "Данные подтверждения колонок не приняты. Проверьте выбранный файл, настройки и подтверждения.") from error
    _, _, inspection = _source(request, upload, body.mapping.sheet)
    if inspection.source_sha256 != body.source_sha256:
        raise ApiError(409, "source_changed", "Исходный прайс изменился. Выберите файл, для которого выполнялась проверка, или проверьте новый заново.")
    if body.mapping.source_sha256 not in (None, body.source_sha256):
        raise ApiError(409, "source_changed", "Настройки колонок относятся к другому файлу. Выберите исходный прайс или сопоставьте колонки заново.")
    try:
        columns = {
            key: value for key, value in body.mapping.model_dump(
                exclude={"source_sha256", "header_row", "sheet", "delimiter",
                         "encoding", "confirmed_currency"},
                exclude_none=True,
            ).items()
        }
        if body.mapping.confirmed_currency != body.confirmed_currency:
            raise ValueError("Reviewed currency confirmation mismatch.")
        if body.mapping.header_row != inspection.header_row or (
            body.mapping.sheet != inspection.selected_sheet
        ):
            raise ValueError("Reviewed source header/sheet mismatch.")
        if body.mapping.delimiter != (inspection.delimiter or ";"):
            raise ValueError("Reviewed delimiter mismatch.")
        if body.mapping.encoding != (inspection.encoding or "utf-8-sig"):
            raise ValueError("Reviewed encoding mismatch.")
        profile = approve_supplier_profile(
            inspection, organization_id=organization_id,
            supplier_id=supplier_id, reviewer=store.identity,
            source_sha256=body.source_sha256,
            selected_columns=columns, price_basis=body.price_basis,
            confirmed_currency=body.confirmed_currency,
            review_evidence=body.evidence, explicitly_confirmed=True,
        )
        store.save(profile)
    except ValueError as error:
        raise ApiError(422, "invalid_mapping_approval", "Заново проверьте колонки, лист, валюту и тип цены, затем подтвердите настройки прайса.") from error
    except TenantAccessDenied as error:
        raise ApiError(403, "mapping_approval_denied", "Не удалось подтвердить настройки колонок. Проверьте доступ и актуальность данных прайса.") from error
    except (RuntimeError, SQLAlchemyError) as error:
        raise ApiError(503, "mapping_store_unavailable", "Не удалось получить или сохранить проверку прайса. Проверьте сохранённые решения перед повторной попыткой.") from error
    response.headers["Cache-Control"] = "no-store"
    return _result(profile, inspection.source_sha256)
