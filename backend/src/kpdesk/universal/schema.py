"""Frozen, bounded canonical DTOs for the separate universal workflow."""
from __future__ import annotations

import hashlib
import json
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_serializer,
    field_validator,
    model_validator,
)

from kpdesk.company.requisites import PartyDetails
from kpdesk.quotes.document import ProposalNarrative
from kpdesk.universal.units import convert, normalized, unit_info

Text = Annotated[str, Field(min_length=1, max_length=2000)]
Quantity = Annotated[Decimal, Field(gt=0, max_digits=15, decimal_places=3, allow_inf_nan=False)]
Money = Annotated[Decimal, Field(ge=0, max_digits=16, decimal_places=2, allow_inf_nan=False)]
SpecNumber = Annotated[Decimal, Field(max_digits=15, decimal_places=6, allow_inf_nan=False)]
Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
_MONEY = TypeAdapter(Money)


def line_total(quantity: Decimal, price_rub: Decimal, price_quantity: Decimal) -> Decimal:
    # The bounded inputs need at most 31 product digits plus 15 divisor digits
    # to distinguish a half-cent boundary. Do not inherit ambient precision.
    with localcontext() as context:
        context.prec = 64
        rounded = (quantity * price_rub / price_quantity).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    return _MONEY.validate_python(rounded)


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    @field_validator("*", mode="before")
    @classmethod
    def safe_scalar(cls, value: object) -> object:
        if isinstance(value, str) and (not value.strip() or "\x00" in value):
            raise ValueError("blank_or_nul_value")
        return value


class Specification(FrozenModel):
    name: Text
    value: Text | SpecNumber
    unit: Text | None = None

    @field_serializer("value", when_used="json")
    def numeric_json_type(self, value: str | Decimal) -> str | int | float:
        # Preserve the text/number distinction through SQL JSON. Bounded to 15
        # significant digits, so JSON binary64 readers retain decimal precision.
        if isinstance(value, str):
            return value
        return int(value) if value == value.to_integral_value() else float(value)


class Requirement(Specification):
    operator: Literal["eq", "min", "max"] = "eq"
    source_quote: Text

    @model_validator(mode="after")
    def numeric_operator(self) -> Requirement:
        if self.operator != "eq" and isinstance(self.value, str):
            raise ValueError("numeric_operator_requires_number")
        return self


class UniversalProduct(FrozenModel):
    sku: Annotated[str, Field(min_length=1, max_length=200)]
    name: Text
    category: Text | None = None
    brand: Text | None = None
    model: Text | None = None
    sale_unit: Annotated[str, Field(min_length=1, max_length=100)]
    price_rub: Money
    price_quantity: Quantity = Decimal(1)
    quantity_step: Quantity | None = None
    specifications: tuple[Specification, ...] = Field(default=(), max_length=64)
    source_row: int | None = Field(default=None, ge=2)

    @model_validator(mode="after")
    def declared_quantity_rules(self) -> UniversalProduct:
        info = unit_info(self.sale_unit)
        if info is None and self.quantity_step is None:
            raise ValueError("unknown_unit_requires_quantity_step")
        if info is not None and info[0] == "count":
            if self.price_quantity != self.price_quantity.to_integral_value():
                raise ValueError("discrete_price_quantity")
            if self.quantity_step is not None and self.quantity_step != self.quantity_step.to_integral_value():
                raise ValueError("discrete_quantity_step")
        return self


class RequestLine(FrozenModel):
    description: Text
    source_quote: Text
    quantity: Quantity | None = None
    sale_unit: Text | None = None
    sku: Text | None = None
    brand: Text | None = None
    model: Text | None = None
    requirements: tuple[Requirement, ...] = Field(default=(), max_length=64)


UniversalRequestLine = RequestLine


class UniversalRequest(FrozenModel):
    lines: tuple[RequestLine, ...] = Field(min_length=1, max_length=20)


class CatalogSnapshot(FrozenModel):
    version_id: UUID
    source_sha256: Sha256
    products: tuple[UniversalProduct, ...] = Field(min_length=1, max_length=5000)

    @model_validator(mode="after")
    def unique_skus(self) -> CatalogSnapshot:
        keys = [normalized(product.sku) for product in self.products]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate_sku")
        return self


class Selection(FrozenModel):
    line_number: int = Field(ge=1, le=20)
    sku: Text


class QuoteContext(FrozenModel):
    issuer_name: Annotated[str, Field(min_length=1, max_length=200)]
    recipient_name: Text | None = None
    manager_brief: Text | None = None
    commercial_terms: Text | None = None
    narrative: ProposalNarrative | None = None
    issuer_details: PartyDetails | None = Field(default=None, exclude_if=lambda value: value is None)
    recipient_details: PartyDetails | None = Field(default=None, exclude_if=lambda value: value is None)
    issuer_contacts: Annotated[str, Field(max_length=600)] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def recipient_matches_requisites(self) -> QuoteContext:
        if self.recipient_details and self.recipient_name not in (
            self.recipient_details.short_name, self.recipient_details.full_name,
        ):
            raise ValueError("recipient_requisites_mismatch")
        return self


class QuoteLine(FrozenModel):
    line_number: int = Field(ge=1, le=20)
    description: Text
    sku: Text | None = None
    name: Text | None = None
    category: Text | None = None
    brand: Text | None = None
    model: Text | None = None
    sale_quantity: Quantity | None = None
    sale_unit: Text | None = None
    price_rub: Money | None = None
    price_quantity: Quantity | None = None
    line_total_rub: Money | None = None
    status: Literal["manager_selected", "needs_review", "incompatible", "unselected"]
    reason_codes: tuple[str, ...] = ()
    specifications: tuple[Specification, ...] = ()


def snapshot_digest(payload: dict[str, object]) -> str:
    body = {key: value for key, value in payload.items() if key != "snapshot_sha256"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


class QuoteSnapshot(QuoteContext):
    quote_id: UUID
    revision: int = Field(ge=1)
    catalog_version_id: UUID
    source_sha256: Sha256
    source_request: UniversalRequest
    selections: tuple[Selection, ...] = Field(max_length=20)
    lines: tuple[QuoteLine, ...] = Field(min_length=1, max_length=20)
    total_rub: Money | None
    ready_for_draft: bool
    approved: Literal[False] = False
    snapshot_sha256: Sha256

    def verify_catalog(self, catalog: CatalogSnapshot) -> None:
        """Verify frozen source facts without reranking with today's matching policy."""
        if (self.catalog_version_id != catalog.version_id or self.source_sha256 != catalog.source_sha256
                or not self.ready_for_draft):
            raise ValueError('universal_quote_catalog_mismatch')
        products = {product.sku: product for product in catalog.products}
        for line in self.lines:
            product = products.get(line.sku) if line.sku is not None else None
            if product is None or any(getattr(line, field) != getattr(product, field) for field in
                                      ('name', 'category', 'brand', 'model', 'sale_unit', 'price_rub',
                                       'price_quantity', 'specifications')):
                raise ValueError('universal_quote_product_mismatch')
            source = self.source_request.lines[line.line_number - 1]
            if (source.quantity is None or source.sale_unit is None
                    or convert(source.quantity, source.sale_unit, product.sale_unit) != line.sale_quantity):
                raise ValueError('universal_quote_quantity_mismatch')

    @model_validator(mode="after")
    def validate_snapshot(self) -> QuoteSnapshot:
        if snapshot_digest(self.model_dump(mode="json")) != self.snapshot_sha256:
            raise ValueError("snapshot_digest_mismatch")
        if len(self.lines) != len(self.source_request.lines):
            raise ValueError("snapshot_line_mismatch")
        expected_numbers = list(range(1, len(self.lines) + 1))
        if [line.line_number for line in self.lines] != expected_numbers:
            raise ValueError("snapshot_line_order_mismatch")
        selected = {selection.line_number: selection.sku for selection in self.selections}
        if len(selected) != len(self.selections) or any(number not in expected_numbers for number in selected):
            raise ValueError("snapshot_selection_mismatch")
        for line in self.lines:
            if line.description != self.source_request.lines[line.line_number - 1].description:
                raise ValueError("snapshot_description_mismatch")
            if line.status == "manager_selected":
                if (selected.get(line.line_number) != line.sku or line.sku is None or line.name is None
                    or line.sale_quantity is None or line.sale_unit is None
                    or line.price_rub is None or line.price_quantity is None
                    or line.line_total_rub is None):
                    raise ValueError("snapshot_selection_mismatch")
                computed = line_total(line.sale_quantity, line.price_rub, line.price_quantity)
                if computed != line.line_total_rub:
                    raise ValueError("snapshot_price_mismatch")
        ready = all(line.status == "manager_selected" and line.line_total_rub is not None
                    for line in self.lines)
        if ready != self.ready_for_draft:
            raise ValueError("snapshot_readiness_mismatch")
        total = sum((line.line_total_rub or Decimal(0) for line in self.lines), Decimal(0))
        if self.total_rub != (total if ready else None):
            raise ValueError("snapshot_total_mismatch")
        return self
