"""Precision review API restricted to the HTTPS tenant application."""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
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
from kpdesk.api.supplier_routes import (
    MappingBody,
    StageResponse,
    SupplierInspectionResponse,
    _safe_uploaded_source,
    _staged_to_response,
)
from kpdesk.api.tenant_session_routes import (
    SESSION_COOKIE,
    _required_session,
    get_unreleased_auth_repository,
)
from kpdesk.catalog.supplier_canonical_preview import (
    CanonicalExportRejected,
    ReviewedPrecisionPrice,
    source_row_sha256,
)
from kpdesk.catalog.supplier_inference import inspect_supplier_source
from kpdesk.catalog.supplier_precision_storage import TenantSupplierPrecisionReviewStore
from kpdesk.catalog.supplier_staging import (
    SupplierMapping,
    pack_size_review_reason,
    stage_supplier_prices,
)
from kpdesk.db.auth_sessions import AuthSessionRepository
from kpdesk.db.tenant_access import TenantAccessDenied

router = APIRouter(prefix="/api/session/organizations/{organization_id}/suppliers/{supplier_id}/precision-reviews")


class PrecisionAttestationBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_row_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mapping: MappingBody
    formula_policy: Literal["reject", "quarantine_rows"] = "reject"
    line: int = Field(ge=1)
    finding: Literal["approve_rounding", "needs_supplier_confirmation", "reject_source_price"]
    rounding_mode: Literal["half_up", "half_even", "down", "up"] | None = None
    rounded_price_rub: str | None = Field(default=None, pattern=r"^(?:0|[1-9][0-9]{0,11})\.[0-9]{2}$")
    rationale: str = Field(min_length=32, max_length=500)


class PrecisionAttestationResponse(BaseModel):
    source_sha256: str
    source_row_sha256: str
    line: int
    recorded: bool
    published: bool = False
    current_price_verified: bool = False
    model_training_authorized: bool = False


class PrecisionReviewRecord(BaseModel):
    source_line: int
    source_row_sha256: str
    source_price_raw: str
    finding: str
    rounding_mode: str | None
    rounded_price_rub: Decimal | None
    rationale: str
    reviewer_user_id: uuid.UUID
    reviewed_at: datetime


class PrecisionReviewList(BaseModel):
    records: list[PrecisionReviewRecord]
    published: bool = False
    current_price_verified: bool = False


class PrecisionSourceCase(BaseModel):
    line: int
    source_row_sha256: str
    supplier_sku: str | None
    name: str
    source_price_raw: str
    source_sale_unit: str | None
    source_pack_size: int | None
    pack_size_review_reason: str | None
    currency_confirmed_rub: bool
    formula_provenance_unverified: bool
    issues: list[str]


class PrecisionSourcePreview(BaseModel):
    source_sha256: str
    source_filename: str
    total_rows: int
    precision_cases: int
    offset: int
    next_offset: int | None
    rows: list[PrecisionSourceCase]
    published: bool = False
    model_training_authorized: bool = False


@router.post("/inspect", response_model=SupplierInspectionResponse)
def inspect_supplier_precision_structure(
    organization_id: uuid.UUID,
    supplier_id: str,
    request: Request,
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    upload: Annotated[UploadFile, File(alias="file")],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    sheet: Annotated[str | None, Form(max_length=100)] = None,
    encoding: Annotated[Literal["utf-8-sig", "cp1251"] | None, Form()] = None,
    formula_policy: Annotated[Literal["reject", "quarantine_rows"], Form()] = "reject",
) -> SupplierInspectionResponse:
    _authorized_supplier_write(
        repository, session_cookie, organization_id, supplier_id, csrf,
    )
    filename, payload = _safe_uploaded_source(request, upload)
    if not filename.casefold().endswith((".csv", ".xls", ".xlsx")):
        raise ApiError(422, "invalid_supplier_source", "Этот формат файла недоступен на данном шаге. Выберите прайс поддерживаемого формата.")
    try:
        inspected = inspect_supplier_source(
            filename, payload, sheet=sheet, encoding=encoding,
            formula_policy=formula_policy,
        )
    except (ValueError, TypeError) as error:
        if str(error) == "Supplier XLSX formula rejected.":
            raise ApiError(
                422, "xlsx_formulas_require_quarantine",
                "В Excel найдены формулы. Включите карантин строк с формулами.",
            ) from error
        raise ApiError(
            422, "source_inspection_required",
            "Не удалось определить структуру прайса. Проверьте файл и сопоставьте колонки вручную.",
        ) from error
    response.headers["Cache-Control"] = "no-store"
    return SupplierInspectionResponse.model_validate(asdict(inspected))


@router.post("/stage", response_model=StageResponse)
def preview_all_supplier_rows(
    organization_id: uuid.UUID,
    supplier_id: str,
    request: Request,
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    upload: Annotated[UploadFile, File(alias="file")],
    mapping_json: Annotated[str, Form()],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    formula_policy: Annotated[
        Literal["reject", "quarantine_rows"], Form(),
    ] = "reject",
    start: Annotated[int, Form(ge=0, le=5000)] = 0,
    limit: Annotated[int, Form(ge=1, le=100)] = 50,
) -> StageResponse:
    """Source-bound tenant staging for ALL rows, never commercial approval."""
    _authorized_supplier_write(
        repository, session_cookie, organization_id, supplier_id, csrf,
    )
    if len(mapping_json) > 4096:
        raise ApiError(422, "invalid_mapping", "Не удалось применить настройки колонок. Проверьте сопоставление полей с исходным прайсом.")
    try:
        fields = MappingBody.model_validate_json(mapping_json)
        mapping = SupplierMapping(**fields.model_dump(exclude={"source_sha256"}))
    except (ValidationError, ValueError, TypeError) as error:
        raise ApiError(422, "invalid_mapping", "Не удалось применить настройки колонок. Проверьте сопоставление полей с исходным прайсом.") from error
    filename, payload = _safe_uploaded_source(request, upload)
    if not filename.casefold().endswith((".csv", ".xls", ".xlsx")):
        raise ApiError(422, "invalid_supplier_source", "Этот формат файла недоступен на данном шаге. Выберите прайс поддерживаемого формата.")
    if fields.source_sha256 is None:
        raise ApiError(422, "source_digest_required", "Сначала проверьте исходный файл прайса, затем повторите действие.")
    if hashlib.sha256(payload).hexdigest() != fields.source_sha256:
        raise ApiError(409, "source_changed", "Исходный прайс изменился. Выберите файл, для которого выполнялась проверка, или проверьте новый заново.")
    try:
        staged = stage_supplier_prices(
            filename, payload, mapping, formula_policy=formula_policy,
        )
    except (ValueError, TypeError) as error:
        raise ApiError(422, "invalid_supplier_source", "Не удалось разобрать строки прайса. Проверьте исходный файл и настройки колонок.") from error
    response.headers["Cache-Control"] = "no-store"
    return _staged_to_response(staged, start=start, limit=limit)


@router.post("/preview", response_model=PrecisionSourcePreview)
def preview_supplier_precision(
    organization_id: uuid.UUID,
    supplier_id: str,
    request: Request,
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    upload: Annotated[UploadFile, File(alias="file")],
    mapping_json: Annotated[str, Form()],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    formula_policy: Annotated[
        Literal["reject", "quarantine_rows"], Form(),
    ] = "reject",
    start: Annotated[int, Form(ge=0, le=5000)] = 0,
    limit: Annotated[int, Form(ge=1, le=100)] = 50,
) -> PrecisionSourcePreview:
    _authorized_supplier_write(
        repository, session_cookie, organization_id, supplier_id, csrf,
    )
    if len(mapping_json) > 4096:
        raise ApiError(422, "invalid_mapping", "Не удалось применить настройки колонок. Проверьте сопоставление полей с исходным прайсом.")
    try:
        fields = MappingBody.model_validate_json(mapping_json)
        mapping = SupplierMapping(**fields.model_dump(exclude={"source_sha256"}))
    except (ValidationError, ValueError, TypeError) as error:
        raise ApiError(422, "invalid_mapping", "Не удалось применить настройки колонок. Проверьте сопоставление полей с исходным прайсом.") from error
    filename, payload = _safe_uploaded_source(request, upload)
    if not filename.lower().endswith((".csv", ".xls", ".xlsx")):
        raise ApiError(422, "invalid_supplier_source", "Этот формат файла недоступен на данном шаге. Выберите прайс поддерживаемого формата.")
    digest = hashlib.sha256(payload).hexdigest()
    if fields.source_sha256 is not None and digest != fields.source_sha256:
        raise ApiError(409, "source_changed", "Исходный прайс изменился. Выберите файл, для которого выполнялась проверка, или проверьте новый заново.")
    try:
        staged = stage_supplier_prices(
            filename, payload, mapping, formula_policy=formula_policy,
        )
    except (ValueError, TypeError) as error:
        raise ApiError(422, "invalid_supplier_source", "Не удалось разобрать формат прайса. Проверьте файл и настройки колонок.") from error
    cases = [item for item in staged.rows if item.source_high_precision_price is not None]
    chosen = cases[start:start + limit]
    response.headers["Cache-Control"] = "no-store"
    return PrecisionSourcePreview(
        source_sha256=staged.sha256,
        source_filename=filename,
        total_rows=len(staged.rows),
        precision_cases=len(cases),
        offset=start,
        next_offset=start + limit if start + limit < len(cases) else None,
        rows=[
            PrecisionSourceCase(
                line=item.line,
                source_row_sha256=source_row_sha256(item),
                supplier_sku=item.supplier_sku,
                name=item.name,
                source_price_raw=item.source_high_precision_price or "",
                source_sale_unit=item.source_sale_unit,
                source_pack_size=item.source_pack_size,
                pack_size_review_reason=pack_size_review_reason(item.issues),
                currency_confirmed_rub="unconfirmed_or_non_rub_currency" not in item.issues,
                formula_provenance_unverified=bool(item.formula_columns) or (
                    "xls_formula_and_macro_provenance_unverified" in item.issues
                ),
                issues=list(item.issues),
            )
            for item in chosen
        ],
    )


def _store(
    repository: AuthSessionRepository,
    session_cookie: str | None,
    organization_id: uuid.UUID,
    *,
    csrf: str | None = None,
    write: bool,
) -> TenantSupplierPrecisionReviewStore:
    identity = _required_session(
        repository, session_cookie, csrf=csrf, write=write,
    )
    return TenantSupplierPrecisionReviewStore(
        connection=repository.connection,
        identity=identity,
        organization_id=organization_id,
        session_token=session_cookie or "",
        csrf_token=csrf or "",
    )


def _authorized_supplier_write(
    repository: AuthSessionRepository,
    session_cookie: str | None,
    organization_id: uuid.UUID,
    supplier_id: str,
    csrf: str | None,
) -> TenantSupplierPrecisionReviewStore:
    """Check session, CSRF and tenant membership before parsing upload bytes."""
    store = _store(repository, session_cookie, organization_id, csrf=csrf, write=True)
    try:
        store._selector(supplier_id, "0" * 64)
        store._authorize(write=True)
    except ValueError as error:
        raise ApiError(422, "invalid_supplier", "Не удалось определить поставщика. Выберите его заново.") from error
    except TenantAccessDenied as error:
        raise ApiError(403, "organization_forbidden", "У вас нет доступа к данным этой организации.") from error
    except (RuntimeError, SQLAlchemyError) as error:
        raise ApiError(
            503, "precision_store_unavailable", "Не удалось получить или сохранить проверку прайса. Проверьте сохранённые решения перед повторной попыткой.",
        ) from error
    return store


@router.post("", response_model=PrecisionAttestationResponse, status_code=201)
def attest_supplier_precision(
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
) -> PrecisionAttestationResponse:
    # Verify session, role and CSRF before reading untrusted supplier bytes.
    store = _authorized_supplier_write(
        repository, session_cookie, organization_id, supplier_id, csrf,
    )

    if len(review_json) > 8_192:
        raise ApiError(422, "invalid_precision_review", "Данные проверки цены не приняты. Проверьте строку, исходную цену, округление и подтверждение.")
    try:
        body = PrecisionAttestationBody.model_validate_json(review_json)
        if body.mapping.source_sha256 not in (None, body.source_sha256):
            raise ValueError("Conflicting source SHA256.")
        if body.finding == "approve_rounding":
            if body.rounding_mode is None or body.rounded_price_rub is None:
                raise ValueError("Explicit price review required.")
            reviewed: ReviewedPrecisionPrice | None = ReviewedPrecisionPrice(
                price_rub=Decimal(body.rounded_price_rub),
                rounding_mode=body.rounding_mode,
                explanation=body.rationale,
            )
        else:
            if body.rounding_mode is not None or body.rounded_price_rub is not None:
                raise ValueError("Unresolved finding must not provide a price.")
            reviewed = None
    except (ValidationError, ValueError, InvalidOperation, TypeError) as error:
        raise ApiError(422, "invalid_precision_review", "Данные проверки цены не приняты. Проверьте строку, исходную цену, округление и подтверждение.") from error

    filename, payload = _safe_uploaded_source(request, upload)
    if hashlib.sha256(payload).hexdigest() != body.source_sha256:
        raise ApiError(409, "source_changed", "Исходный прайс изменился. Выберите файл, для которого выполнялась проверка, или проверьте новый заново.")
    try:
        mapping = SupplierMapping(
            **body.mapping.model_dump(exclude={"source_sha256"})
        )
        if not filename.casefold().endswith((".csv", ".xlsx", ".xls")):
            raise ValueError("Unsupported price-source format.")
        staged = stage_supplier_prices(
            filename, payload, mapping, formula_policy=body.formula_policy,
        )
        if staged.sha256 != body.source_sha256:
            raise ValueError("Source mismatch.")
        row = next((item for item in staged.rows if item.line == body.line), None)
        if row is None:
            raise ValueError("Source row missing.")
        if source_row_sha256(row) != body.source_row_sha256:
            raise ApiError(409, "source_row_changed", "Исходная строка изменилась или больше не найдена. Заново проверьте цену и данные строки.")
    except ApiError:
        raise
    except (ValueError, TypeError) as error:
        raise ApiError(422, "invalid_supplier_source", "Не удалось разобрать исходный прайс. Проверьте файл и настройки колонок.") from error

    try:
        store.attest(
            staged, supplier_id, row,
            finding=body.finding,
            precision_review=reviewed,
            rationale=body.rationale,
        )
    except CanonicalExportRejected as error:
        raise ApiError(422, "price_precision_rejected", "Не удалось подтвердить проверку цены. Проверьте исходную цену, округление и обоснование.") from error
    except ValueError as error:
        raise ApiError(422, "invalid_precision_review", "Не удалось подтвердить проверку цены. Проверьте исходную цену, округление и обоснование.") from error
    except TenantAccessDenied as error:
        raise ApiError(403, "precision_attestation_denied", "Не удалось подтвердить проверку цены. Проверьте доступ и актуальность данных прайса.") from error
    except (SQLAlchemyError, RuntimeError) as error:
        raise ApiError(503, "precision_store_unavailable", "Не удалось получить или сохранить проверку прайса. Проверьте сохранённые решения перед повторной попыткой.") from error

    response.headers["Cache-Control"] = "no-store"
    return PrecisionAttestationResponse(
        source_sha256=staged.sha256,
        source_row_sha256=body.source_row_sha256,
        line=row.line,
        recorded=True,
    )


@router.get("", response_model=PrecisionReviewList)
def list_supplier_precision_reviews(
    organization_id: uuid.UUID,
    supplier_id: str,
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    source_sha256: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    line: Annotated[int | None, Query(ge=1)] = None,
) -> PrecisionReviewList:
    store = _store(repository, session_cookie, organization_id, write=False)
    try:
        records = store.list_reviews(supplier_id, source_sha256)
    except ValueError as error:
        raise ApiError(422, "invalid_supplier", "Не удалось определить поставщика или версию его прайса. Выберите их заново.") from error
    except TenantAccessDenied as error:
        raise ApiError(403, "organization_forbidden", "У вас нет доступа к данным этой организации.") from error
    except (SQLAlchemyError, RuntimeError) as error:
        raise ApiError(503, "precision_store_unavailable", "Не удалось получить или сохранить проверку прайса. Проверьте сохранённые решения перед повторной попыткой.") from error
    if line is not None:
        records = [record for record in records if record["source_line"] == line]
    if len(records) > 100:
        raise ApiError(422, "source_review_page_required", "Для этого прайса найдено много проверок цены. Выберите номер исходной строки, чтобы загрузить её историю.")
    response.headers["Cache-Control"] = "no-store"
    return PrecisionReviewList(
        records=[PrecisionReviewRecord.model_validate(record) for record in records],
    )
