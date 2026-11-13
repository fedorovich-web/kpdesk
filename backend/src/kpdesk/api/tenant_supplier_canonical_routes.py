"""Source-bound reviewed supplier export for the HTTPS tenant workspace.

No database writes, catalog publication or model training. The returned CSV
must pass the separate tenant catalog preflight and explicit publish action.
"""
from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from fastapi import APIRouter, Cookie, Depends, File, Form, Header, Request, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kpdesk.api.errors import ApiError
from kpdesk.api.supplier_routes import MappingBody, _safe_uploaded_source
from kpdesk.api.tenant_session_routes import (
    SESSION_COOKIE,
    get_unreleased_auth_repository,
)
from kpdesk.api.tenant_supplier_precision_routes import _authorized_supplier_write
from kpdesk.catalog.supplier_canonical_preview import (
    CanonicalExportRejected,
    ReviewedExclusion,
    ReviewedPrecisionPrice,
    ReviewedProductDecision,
    preview_canonical_catalog,
)
from kpdesk.catalog.supplier_profiles import PriceBasis
from kpdesk.catalog.supplier_staging import SupplierMapping, stage_supplier_prices
from kpdesk.db.auth_sessions import AuthSessionRepository

router = APIRouter(
    prefix="/api/session/organizations/{organization_id}/suppliers/{supplier_id}/canonical",
)


class PrecisionDecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    price_rub: str = Field(pattern=r"^(?:0|[1-9][0-9]{0,11})\.[0-9]{2}$")
    rounding_mode: Literal["half_up", "half_even", "down", "up"]
    explanation: str = Field(min_length=32, max_length=500)


class IncludeBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["include"]
    line: int = Field(ge=1)
    source_row_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed: Literal[True]
    category: str = Field(min_length=1, max_length=60)
    sale_unit: Literal["штука", "пачка", "коробка"]
    pieces_per_sale_unit: int = Field(ge=1, le=100000)
    price_unit_review_evidence: str | None = Field(default=None, max_length=500)
    precision_review: PrecisionDecisionBody | None = None
    brand: str | None = Field(default=None, max_length=500)
    paper_format: str | None = Field(default=None, max_length=100)
    paper_gsm: int | None = Field(default=None, ge=1, le=1000)
    pen_ink_color: str | None = Field(default=None, max_length=100)
    pen_ink_type: str | None = Field(default=None, max_length=100)
    attributes: dict[str, str | int | list[str]] = Field(default_factory=dict)


class ExcludeBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["exclude"]
    line: int = Field(ge=1)
    source_row_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed: Literal[True]
    reason: str = Field(min_length=8, max_length=500)


class CanonicalExportBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mapping: MappingBody
    formula_policy: Literal["reject", "quarantine_rows"] = "reject"
    price_basis: PriceBasis
    explicitly_confirmed_price_basis: Literal[True]
    decisions: list[Annotated[IncludeBody | ExcludeBody, Field(discriminator="action")]] = Field(
        min_length=1, max_length=5000,
    )



def reviewed_decisions(
    items: list[IncludeBody | ExcludeBody],
) -> tuple[ReviewedProductDecision | ReviewedExclusion, ...]:
    """One typed conversion for both final export and saved partial review."""
    result: list[ReviewedProductDecision | ReviewedExclusion] = []
    for item in items:
        if isinstance(item, ExcludeBody):
            result.append(ReviewedExclusion(
                line=item.line, source_row_sha256=item.source_row_sha256,
                confirmed=True, reason=item.reason,
            ))
            continue
        precision: ReviewedPrecisionPrice | None = None
        if item.precision_review is not None:
            try:
                precision = ReviewedPrecisionPrice(
                    price_rub=Decimal(item.precision_review.price_rub),
                    rounding_mode=item.precision_review.rounding_mode,
                    explanation=item.precision_review.explanation,
                )
            except InvalidOperation as error:
                raise ApiError(
                    422, "invalid_canonical_review", "Проверка точности цены не пройдена. Проверьте исходную цену, округление и подтверждение.",
                ) from error
        result.append(ReviewedProductDecision(
            line=item.line, source_row_sha256=item.source_row_sha256,
            confirmed=True, category=item.category, sale_unit=item.sale_unit,
            pieces_per_sale_unit=item.pieces_per_sale_unit,
            price_unit_review_evidence=item.price_unit_review_evidence,
            precision_review=precision, brand=item.brand,
            paper_format=item.paper_format, paper_gsm=item.paper_gsm,
            pen_ink_color=item.pen_ink_color, pen_ink_type=item.pen_ink_type,
            attributes=item.attributes,
        ))
    return tuple(result)


@router.post("/export")
def export_reviewed_supplier_catalog(
    organization_id: uuid.UUID,
    supplier_id: str,
    request: Request,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    upload: Annotated[UploadFile, File(alias="file")],
    review_json: Annotated[str, Form()],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> Response:
    # Session, CSRF and tenant write scope are checked before reading the file.
    scope = _authorized_supplier_write(
        repository, session_cookie, organization_id, supplier_id, csrf,
    )
    if len(review_json) > 4 * 1024 * 1024:
        raise ApiError(413, "supplier_review_too_large", "Превышен допустимый объём проверки товаров. Сократите число решений в одном запросе.")
    try:
        body = CanonicalExportBody.model_validate_json(review_json)
        if body.mapping.source_sha256 not in (None, body.source_sha256):
            raise ValueError("Inconsistent original source digest.")
        mapping = SupplierMapping(**body.mapping.model_dump(exclude={"source_sha256"}))
    except (ValidationError, ValueError, TypeError) as error:
        raise ApiError(422, "invalid_canonical_review", "Решения по товарам не прошли проверку. Проверьте выбранные строки и обязательные поля.") from error

    filename, payload = _safe_uploaded_source(request, upload)
    if not filename.casefold().endswith((".csv", ".xlsx", ".xls")):
        raise ApiError(422, "invalid_supplier_source", "Этот формат файла недоступен на данном шаге. Выберите прайс поддерживаемого формата.")
    if hashlib.sha256(payload).hexdigest() != body.source_sha256:
        raise ApiError(409, "source_changed", "Исходный прайс изменился. Выберите файл, для которого выполнялась проверка, или проверьте новый заново.")
    try:
        staged = stage_supplier_prices(
            filename, payload, mapping, formula_policy=body.formula_policy,
        )
    except (ValueError, TypeError) as error:
        raise ApiError(422, "invalid_supplier_source", "Не удалось разобрать строки прайса. Проверьте исходный файл и настройки колонок.") from error

    decisions = reviewed_decisions(body.decisions)
    try:
        preview = preview_canonical_catalog(
            staged, tuple(decisions), organization_id=organization_id,
            reviewer=scope.identity, expected_source_sha256=body.source_sha256,
            price_basis=body.price_basis, explicitly_confirmed_price_basis=True,
        )
    except CanonicalExportRejected as error:
        raise ApiError(
            422, error.code, "Товар не прошёл проверку для загрузки в каталог. Проверьте его параметры и подтверждения.", row=error.line,
        ) from error
    return Response(
        content=preview.csv_content, media_type="text/csv; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'attachment; filename="reviewed-supplier.csv"',
            "X-Catalog-SHA256": preview.csv_sha256,
            "X-Source-SHA256": preview.source_sha256,
            "X-Excluded-Rows": ",".join(str(line) for line in preview.excluded_source_rows),
        },
    )
