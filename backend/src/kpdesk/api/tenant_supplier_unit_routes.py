"""HTTPS tenant-only audit for human-review of supplier unit/pack denominators."""
from __future__ import annotations

import hashlib
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
    get_unreleased_auth_repository,
)
from kpdesk.api.tenant_supplier_precision_routes import (
    _authorized_supplier_write,
    _store,
)
from kpdesk.catalog.supplier_canonical_preview import CanonicalUnit, source_row_sha256
from kpdesk.catalog.supplier_staging import SupplierMapping, stage_supplier_prices
from kpdesk.catalog.supplier_unit_storage import TenantSupplierUnitReviewStore, UnitFinding
from kpdesk.db.auth_sessions import AuthSessionRepository
from kpdesk.db.tenant_access import TenantAccessDenied

router = APIRouter(
    prefix="/api/session/organizations/{organization_id}/suppliers/{supplier_id}/unit-reviews",
)


class UnitAttestationBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_row_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mapping: MappingBody
    formula_policy: Literal["reject", "quarantine_rows"] = "reject"
    line: int = Field(ge=1)
    finding: UnitFinding
    approved_sale_unit: CanonicalUnit | None = None
    approved_pack_size: int | None = Field(default=None, ge=1, le=100000)
    rationale: str = Field(min_length=32, max_length=500)
    personally_reviewed: Literal[True]


class UnitAttestationResponse(BaseModel):
    source_sha256: str
    source_row_sha256: str
    line: int
    recorded: bool
    published: bool = False
    model_training_authorized: bool = False


class UnitReviewRecord(BaseModel):
    source_line: int
    source_row_sha256: str
    source_sale_unit: str | None
    source_pack_size: int | None
    finding: UnitFinding
    approved_sale_unit: CanonicalUnit | None
    approved_pack_size: int | None
    rationale: str
    reviewer_user_id: uuid.UUID
    reviewed_at: datetime


class UnitReviewHistory(BaseModel):
    records: list[UnitReviewRecord]
    next_offset: int | None
    published: bool = False
    model_training_authorized: bool = False


@router.post("", response_model=UnitAttestationResponse, status_code=201)
def attest_supplier_unit(
    organization_id: uuid.UUID,
    supplier_id: str,
    request: Request,
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    upload: Annotated[UploadFile, File(alias="file")],
    review_json: Annotated[str, Form()],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> UnitAttestationResponse:
    scope = _authorized_supplier_write(
        repository, session_cookie, organization_id, supplier_id, csrf,
    )
    if len(review_json) > 8192:
        raise ApiError(422, "invalid_unit_review", "Данные проверки единиц продажи превышают допустимый объём. Проверьте поля и сократите пояснение.")
    try:
        body = UnitAttestationBody.model_validate_json(review_json)
        if body.mapping.source_sha256 not in (None, body.source_sha256):
            raise ValueError("Inconsistent source digest.")
        mapping = SupplierMapping(**body.mapping.model_dump(exclude={"source_sha256"}))
    except (ValueError, TypeError, ValidationError) as error:
        raise ApiError(422, "invalid_unit_review", "Данные проверки единиц продажи не приняты. Проверьте исходную строку, единицу продажи и количество в упаковке.") from error
    filename, payload = _safe_uploaded_source(request, upload)
    if not filename.casefold().endswith((".csv", ".xlsx", ".xls")):
        raise ApiError(422, "invalid_supplier_source", "Этот формат файла недоступен на данном шаге. Выберите прайс поддерживаемого формата.")
    if hashlib.sha256(payload).hexdigest() != body.source_sha256:
        raise ApiError(409, "source_changed", "Исходный прайс изменился. Выберите файл, для которого выполнялась проверка, или проверьте новый заново.")
    try:
        staged = stage_supplier_prices(
            filename, payload, mapping, formula_policy=body.formula_policy,
        )
        row = next((item for item in staged.rows if item.line == body.line), None)
        if row is None:
            raise ApiError(409, "source_row_changed", "Исходная строка больше не найдена в прайсе. Выберите исходный файл и проверьте строку заново.")
        if source_row_sha256(row) != body.source_row_sha256:
            raise ApiError(409, "source_row_changed", "Исходная строка изменилась. Заново проверьте единицу продажи и количество в упаковке.")
    except ApiError:
        raise
    except (ValueError, TypeError) as error:
        raise ApiError(422, "invalid_supplier_source", "Не удалось разобрать исходный прайс. Проверьте файл и настройки колонок.") from error
    try:
        TenantSupplierUnitReviewStore(scope).attest(
            staged, supplier_id, row,
            finding=body.finding, approved_sale_unit=body.approved_sale_unit,
            approved_pack_size=body.approved_pack_size, rationale=body.rationale,
        )
    except ValueError as error:
        raise ApiError(422, "invalid_unit_review", "Не удалось подтвердить единицу продажи и количество в упаковке. Проверьте данные исходной строки и обоснование.") from error
    except TenantAccessDenied as error:
        raise ApiError(403, "unit_attestation_denied", "Не удалось подтвердить проверку единиц продажи. Проверьте доступ и актуальность данных прайса.") from error
    except (RuntimeError, SQLAlchemyError) as error:
        raise ApiError(503, "unit_store_unavailable", "Не удалось получить или сохранить проверку единиц продажи. Проверьте сохранённые решения перед повторной попыткой.") from error
    response.headers["Cache-Control"] = "no-store"
    return UnitAttestationResponse(
        source_sha256=staged.sha256, source_row_sha256=body.source_row_sha256,
        line=row.line, recorded=True,
    )


@router.get("", response_model=UnitReviewHistory)
def list_supplier_unit_reviews(
    organization_id: uuid.UUID,
    supplier_id: str,
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    source_sha256: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 50,
    offset: Annotated[int, Query(ge=0, le=5000)] = 0,
) -> UnitReviewHistory:
    scope = _store(repository, session_cookie, organization_id, write=False)
    try:
        records = TenantSupplierUnitReviewStore(scope).list_reviews(
            supplier_id, source_sha256, limit=limit + 1, offset=offset,
        )
    except ValueError as error:
        raise ApiError(422, "invalid_unit_review", "Не удалось определить прайс или строку для проверки единиц продажи. Выберите их заново.") from error
    except TenantAccessDenied as error:
        raise ApiError(403, "organization_forbidden", "У вас нет доступа к данным этой организации.") from error
    except (RuntimeError, SQLAlchemyError) as error:
        raise ApiError(503, "unit_store_unavailable", "Не удалось загрузить историю проверки единиц продажи. Попробуйте позже.") from error
    response.headers["Cache-Control"] = "no-store"
    return UnitReviewHistory(
        records=[UnitReviewRecord.model_validate(row) for row in records[:limit]],
        next_offset=offset + limit if len(records) > limit else None,
    )
