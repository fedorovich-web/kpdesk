"""Server-side quote calculation preview; no database writes or approvals.

Prices always come from a single active, version-checked catalog snapshot.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from kpdesk.api.errors import ApiError
from kpdesk.api.routes import CatalogRepository, get_catalog_repository
from kpdesk.extraction.schema import ExtractedRequest
from kpdesk.extraction.to_matching import ExtractionNeedsReview, to_requested_item
from kpdesk.matching.schema import RequestedItem, SaleUnit
from kpdesk.quotes.auto_draft import build_auto_draft
from kpdesk.quotes.calculate import (
    LineStatus,
    QuoteCalculationError,
    calculate_line,
    calculate_quote,
)
from kpdesk.quotes.instructions import (
    ConfirmedInstructions,
    InstructionProposal,
    check_confirmation,
    filter_catalog_by_brand,
    propose_instructions,
    selling_total,
)

router = APIRouter(prefix="/api/quotes")


class QuoteLineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sku: str = Field(min_length=1, max_length=100)
    requested_item: RequestedItem
    approved_sale_quantity: int | None = Field(default=None, ge=1, le=100000)


class QuoteCalculateBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog_version_id: uuid.UUID
    lines: list[QuoteLineRequest] = Field(min_length=1, max_length=20)


class QuoteLineResponse(BaseModel):
    sku: str
    status: LineStatus
    requested_quantity: int
    requested_unit: SaleUnit
    sale_unit: SaleUnit
    pieces_per_sale_unit: int
    price_rub: Decimal
    sale_quantity: int | None
    suggested_sale_quantity: int | None
    delivered_pieces: int | None
    overage_pieces: int | None
    line_total_rub: Decimal | None
    requires_explicit_pack_confirmation: bool
    reason_codes: tuple[str, ...]


class QuoteCalculateResponse(BaseModel):
    catalog_version_id: uuid.UUID
    ready: bool
    requires_manager_confirmation: bool = True
    total_rub: Decimal | None
    lines: list[QuoteLineResponse]


@router.post("/calculate", response_model=QuoteCalculateResponse)
def api_calculate_quote(
    body: QuoteCalculateBody,
    repository: Annotated[CatalogRepository, Depends(get_catalog_repository)],
) -> QuoteCalculateResponse:
    active = repository.current()
    if active is None:
        raise ApiError(404, "no_active_catalog", "Сначала загрузите прайс.")
    if active.id != body.catalog_version_id:
        raise ApiError(
            409, "stale_catalog", "Активный прайс изменился. Обновите подбор и проверьте цены."
        )
    catalog = {product.sku: product for product in repository.items(active.id)}
    calculated = []
    for row_number, line in enumerate(body.lines, start=1):
        product = catalog.get(line.sku)
        if product is None:
            raise ApiError(
                422, "invalid_quote_line", "Выбранный товар не найден в активном прайсе. Обновите подбор.",
                row=row_number,
            )
        try:
            calculation = calculate_line(
                line.requested_item, product, line.approved_sale_quantity,
            )
        except QuoteCalculationError as error:
            raise ApiError(
                422, "invalid_quote_line", "Не удалось рассчитать позицию. Проверьте товар, количество и единицу продажи.",
                row=row_number,
            ) from error
        calculated.append(calculation)
    is_ready = all(line.status == "ready" for line in calculated)
    amount: Decimal | None = None
    if is_ready:
        try:
            amount = calculate_quote(calculated).total_rub
        except QuoteCalculationError as error:
            raise ApiError(422, "invalid_quote", "Не удалось проверить общую сумму КП. Проверьте состав и расчёт предложения.") from error
    return QuoteCalculateResponse(
        catalog_version_id=active.id,
        ready=is_ready,
        total_rub=amount,
        lines=[QuoteLineResponse.model_validate(asdict(item)) for item in calculated],
    )



class InstructionPreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)


@router.post("/instructions/preview", response_model=InstructionProposal)
def api_preview_instructions(body: InstructionPreviewBody) -> InstructionProposal:
    try:
        return propose_instructions(body.text)
    except ValueError as error:
        raise ApiError(422, "invalid_instructions", "Не удалось применить условия подбора и расчёта. Проверьте текст правил.") from error


class AutoDraftBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    catalog_version_id: uuid.UUID
    extracted_request: ExtractedRequest
    instructions: ConfirmedInstructions | None = None


class AutoDraftLineResponse(BaseModel):
    line_number: int
    status: Literal["auto_selected", "needs_review", "no_match"]
    sku: str | None
    line_total_rub: Decimal | None
    reason_codes: tuple[str, ...]
    selection_method: str | None


class AutoDraftResponse(BaseModel):
    catalog_version_id: uuid.UUID
    ready_for_draft: bool
    total_rub: Decimal | None
    base_total_rub: Decimal | None = None
    markup_percent: Decimal = Decimal(0)
    budget_max_rub: Decimal | None = None
    budget_exceeded: bool = False
    approval_status: Literal["not_approved"] = "not_approved"
    lines: list[AutoDraftLineResponse]


@router.post("/auto-draft", response_model=AutoDraftResponse)
def api_auto_draft(
    body: AutoDraftBody,
    repository: Annotated[CatalogRepository, Depends(get_catalog_repository)],
) -> AutoDraftResponse:
    active = repository.current()
    if active is None:
        raise ApiError(404, "no_active_catalog", "Сначала загрузите прайс.")
    if active.id != body.catalog_version_id:
        raise ApiError(409, "stale_catalog", "Активный прайс изменился. Обновите подбор и проверьте цены.")
    if not body.extracted_request.lines:
        raise ApiError(422, "empty_request", "В заявке нет позиций для подбора. Добавьте товары и требования клиента.")
    requests: list[RequestedItem | None] = []
    for line in body.extracted_request.lines:
        try:
            requests.append(to_requested_item(line))
        except ExtractionNeedsReview:
            requests.append(None)
    try:
        rules = (
            check_confirmation(body.instructions) if body.instructions is not None
            else None
        )
    except ValueError as error:
        raise ApiError(
            422, "invalid_instructions", "Правила подбора и расчёта изменены или не подтверждены. Проверьте и подтвердите их заново.",
        ) from error
    try:
        draft = build_auto_draft(
            requests, filter_catalog_by_brand(repository.items(active.id), rules),
        )
        markup = rules.markup_percent if rules else Decimal(0)
        adjusted_lines = [
            AutoDraftLineResponse(
                line_number=line.line_number,
                status=line.status,
                sku=line.sku,
                line_total_rub=(
                    selling_total(line.line_total_rub, markup)
                    if line.line_total_rub is not None else None
                ),
                reason_codes=line.reason_codes,
                selection_method=line.selection_method,
            )
            for line in draft.lines
        ]
        adjusted_total = (
            sum(
                (line.line_total_rub for line in adjusted_lines
                 if line.line_total_rub is not None),
                Decimal("0.00"),
            )
            if draft.ready_for_draft else None
        )
        budget = rules.budget_max_rub if rules else None
        exceeded = adjusted_total is not None and budget is not None and adjusted_total > budget
    except ValueError as error:
        raise ApiError(422, "invalid_draft", "Не удалось подготовить черновик КП. Проверьте заявку и условия расчёта.") from error
    return AutoDraftResponse(
        catalog_version_id=active.id,
        ready_for_draft=draft.ready_for_draft and not exceeded,
        total_rub=adjusted_total,
        base_total_rub=draft.total_rub,
        markup_percent=markup,
        budget_max_rub=budget,
        budget_exceeded=exceeded,
        lines=adjusted_lines,
    )
