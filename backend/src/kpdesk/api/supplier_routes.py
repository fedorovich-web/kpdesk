"""Local owner-authenticated read-only preview for noncanonical supplier files.

Never publishes catalog versions, calls LLM providers or writes to PostgreSQL.
No unauthenticated arbitrary-file parsing.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kpdesk.api.errors import ApiError
from kpdesk.api.persisted_quote_routes import require_owner
from kpdesk.catalog.supplier_canonical_preview import source_row_sha256
from kpdesk.catalog.supplier_commerceml import stage_commerceml
from kpdesk.catalog.supplier_inference import inspect_supplier_source
from kpdesk.catalog.supplier_json import JsonSupplierMapping, stage_json_supplier_prices
from kpdesk.catalog.supplier_staging import (
    PACK_SIZE_REVIEW_MESSAGES,
    SupplierMapping,
    SupplierStaging,
    pack_size_review_reason,
    stage_supplier_prices,
)

router = APIRouter(
    prefix="/api/catalog/suppliers",
    dependencies=[Depends(require_owner)],
)


class MappingBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: int = Field(ge=0, lt=64)
    price: int = Field(ge=0, lt=64)
    header_row: int = Field(ge=1, le=20)
    sku: int | None = Field(default=None, ge=0, lt=64)
    currency: int | None = Field(default=None, ge=0, lt=64)
    category: int | None = Field(default=None, ge=0, lt=64)
    sale_unit: int | None = Field(default=None, ge=0, lt=64)
    pieces_per_sale_unit: int | None = Field(default=None, ge=0, lt=64)
    confirmed_currency: Literal["RUB"] | None = None
    source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    sheet: str | None = Field(default=None, min_length=1, max_length=100)
    encoding: Literal["utf-8", "utf-8-sig", "cp1251"] = "utf-8-sig"
    delimiter: Literal[";", ",", "\t"] = ";"


class JsonMappingBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1, max_length=100)
    price: str = Field(min_length=1, max_length=100)
    sku: str | None = Field(default=None, min_length=1, max_length=100)
    currency: str | None = Field(default=None, min_length=1, max_length=100)
    category: str | None = Field(default=None, min_length=1, max_length=100)
    sale_unit: str | None = Field(default=None, min_length=1, max_length=100)
    pieces_per_sale_unit: str | None = Field(default=None, min_length=1, max_length=100)
    items_key: Literal["items", "products", "data", "offers"] | None = None
    confirmed_currency: Literal["RUB"] | None = None
    source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class SupplierInspectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_sha256: str
    source_format: str
    row_count: int
    available_sheets: tuple[str, ...]
    selected_sheet: str | None
    encoding: str | None
    delimiter: str | None
    items_key: str | None
    header_row: int | None
    observed_columns: tuple[str, ...]
    sample_rows: tuple[tuple[str, ...], ...]
    proposed_fields: dict[str, int | str]
    options: dict[str, tuple[int | str, ...]]
    stage_mapping: dict[str, int | str | None] | None
    issues: tuple[str, ...]
    requires_confirmation: bool
    canonical_importable: bool
    model_training_authorized: bool


class StagedItemResponse(BaseModel):
    line: int
    source_row_sha256: str
    supplier_sku: str | None
    name: str
    price_rub: str | None
    source_high_precision_price: str | None
    source_category: str | None
    source_sale_unit: str | None
    source_pack_size: int | None
    issues: tuple[str, ...]
    pack_size_review_reason: str | None


class StageResponse(BaseModel):
    source_sha256: str
    total_rows: int
    priced_rub_rows: int
    unresolved_rows: int
    pack_size_issue_counts: dict[str, int]
    offset: int
    next_offset: int | None
    sampled_rows: tuple[StagedItemResponse, ...]
    requires_confirmation: bool = True
    canonical_importable: bool = False
    model_training_authorized: bool = False


class CommerceMLStageResponse(BaseModel):
    stage: StageResponse
    price_type_id: str | None
    offered_price_type_ids: tuple[str, ...]
    requires_confirmation: bool = True
    published: bool = False


class AutoStageResponse(BaseModel):
    inspection: SupplierInspectionResponse
    staging: StageResponse | None
    requires_confirmation: bool = True
    published: bool = False


def _staged_to_response(
    staged: SupplierStaging, *, start: int = 0, limit: int = 20,
) -> StageResponse:
    samples = tuple(
        StagedItemResponse(
            line=item.line,
            source_row_sha256=source_row_sha256(item),
            supplier_sku=item.supplier_sku,
            name=item.name,
            price_rub=str(item.price_rub) if item.price_rub is not None else None,
            source_high_precision_price=item.source_high_precision_price,
            source_category=item.source_category,
            source_sale_unit=item.source_sale_unit,
            source_pack_size=item.source_pack_size,
            issues=item.issues,
            pack_size_review_reason=pack_size_review_reason(item.issues),
        ) for item in staged.rows[start:start + limit]
    )
    return StageResponse(
        source_sha256=staged.sha256,
        total_rows=len(staged.rows),
        priced_rub_rows=sum(row.price_rub is not None for row in staged.rows),
        unresolved_rows=sum(bool(row.issues) for row in staged.rows),
        pack_size_issue_counts={
            code: sum(code in row.issues for row in staged.rows)
            for code in PACK_SIZE_REVIEW_MESSAGES
        },
        offset=start,
        next_offset=start + limit if start + limit < len(staged.rows) else None,
        sampled_rows=samples,
    )


def _safe_uploaded_source(request: Request, file: UploadFile) -> tuple[str, bytes]:
    filename = file.filename or ""
    if (
        not filename or len(filename) > 250
        or "/" in filename or "\\" in filename
        or any(ord(character) < 32 for character in filename)
    ):
        raise ApiError(422, "invalid_filename", "Имя файла прайса недопустимо. Переименуйте файл и выберите его заново.")
    maximum = request.app.state.settings.max_upload_bytes
    payload = file.file.read(maximum + 1)
    if len(payload) > maximum:
        raise ApiError(413, "source_too_large", "Размер файла прайса превышает лимит загрузки.")
    return filename, payload


@router.post("/commerceml-stage", response_model=CommerceMLStageResponse)
def stage_commerceml_supplier(
    request: Request,
    file: Annotated[UploadFile, File()],
    price_type_id: Annotated[str | None, Form()] = None,
) -> CommerceMLStageResponse:
    filename, payload = _safe_uploaded_source(request, file)
    try:
        source = stage_commerceml(filename, payload, price_type_id=price_type_id)
    except (ValueError, TypeError) as error:
        # Keep exact source contents and third-party XML out of error responses.
        raise ApiError(
            422, "commerceml_review_required",
            "Не удалось разобрать прайс CommerceML. Проверьте структуру файла и выбранный тип цены.",
        ) from error
    staging = SupplierStaging(source.source_sha256, filename, source.rows)
    return CommerceMLStageResponse(
        stage=_staged_to_response(staging),
        price_type_id=source.selected_price_type_id,
        offered_price_type_ids=source.offered_price_type_ids,
    )


@router.post("/inspect", response_model=SupplierInspectionResponse)
def inspect_supplier(
    request: Request,
    file: Annotated[UploadFile, File()],
    sheet: Annotated[str | None, Form()] = None,
    encoding: Annotated[str | None, Form()] = None,
) -> SupplierInspectionResponse:
    filename, payload = _safe_uploaded_source(request, file)
    try:
        preview = inspect_supplier_source(
            filename, payload, sheet=sheet, encoding=encoding,
        )
    except (ValueError, TypeError) as error:
        raise ApiError(
            422, "invalid_supplier_source", "Не удалось прочитать структуру прайса. Проверьте формат и содержимое файла.",
        ) from error
    return SupplierInspectionResponse.model_validate(asdict(preview))


@router.post("/auto-stage", response_model=AutoStageResponse)
def auto_stage_supplier(
    request: Request,
    file: Annotated[UploadFile, File()],
    sheet: Annotated[str | None, Form()] = None,
    encoding: Annotated[str | None, Form()] = None,
) -> AutoStageResponse:
    """One offline upload: structural inference then typed staging without publication."""
    filename, payload = _safe_uploaded_source(request, file)
    try:
        inspected = inspect_supplier_source(
            filename, payload, sheet=sheet, encoding=encoding,
        )
        inspection = SupplierInspectionResponse.model_validate(asdict(inspected))
        proposal = inspected.stage_mapping
        if proposal is None:
            return AutoStageResponse(inspection=inspection, staging=None)
        if inspected.source_format == "json":
            staged = stage_json_supplier_prices(
                filename, payload, JsonSupplierMapping(**proposal),
            )
        else:
            staged = stage_supplier_prices(
                filename, payload, SupplierMapping(**proposal),
            )
    except (ValueError, TypeError) as error:
        raise ApiError(
            422, "invalid_supplier_source", "Не удалось автоматически разобрать прайс. Проверьте структуру файла и сопоставьте колонки вручную.",
        ) from error
    return AutoStageResponse(
        inspection=inspection, staging=_staged_to_response(staged),
    )


@router.post("/stage", response_model=StageResponse)
def stage_supplier(
    request: Request,
    file: Annotated[UploadFile, File()],
    mapping_json: Annotated[str, Form()],
    start: Annotated[int, Form(ge=0, le=5000)] = 0,
    limit: Annotated[int, Form(ge=1, le=100)] = 20,
) -> StageResponse:
    if len(mapping_json) > 4096:
        raise ApiError(422, "invalid_mapping", "Настройки колонок превышают допустимый объём. Проверьте сопоставление полей прайса.")
    filename = file.filename or ""
    try:
        if filename.casefold().endswith(".json"):
            parsed_json = JsonMappingBody.model_validate_json(mapping_json)
            json_mapping = JsonSupplierMapping(
                **parsed_json.model_dump(exclude={"source_sha256"})
            )
            expected_hash = parsed_json.source_sha256
            mapping = None
        else:
            parsed = MappingBody.model_validate_json(mapping_json)
            mapping = SupplierMapping(
                **parsed.model_dump(exclude={"source_sha256"})
            )
            expected_hash = parsed.source_sha256
            json_mapping = None
    except (ValidationError, ValueError, TypeError):
        raise ApiError(422, "invalid_mapping", "Проверьте и подтвердите сопоставление колонок прайса.") from None
    filename, payload = _safe_uploaded_source(request, file)
    if expected_hash is not None and hashlib.sha256(payload).hexdigest() != expected_hash:
        raise ApiError(409, "source_changed", "Файл изменился после проверки. Выберите исходный прайс или проверьте новый файл заново.")
    try:
        if json_mapping is not None:
            staged = stage_json_supplier_prices(filename, payload, json_mapping)
        elif mapping is not None:
            staged = stage_supplier_prices(filename, payload, mapping)
        else:
            raise ValueError("Mapping is required.")
    except ValueError as error:
        raise ApiError(422, "invalid_source", "Не удалось разобрать строки прайса. Проверьте исходный файл, настройки колонок и формат значений.") from error
    return _staged_to_response(staged, start=start, limit=limit)
