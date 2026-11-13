"""Shared typed contracts for catalog matching and evaluation."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from kpdesk.matching.profiles import CATEGORY_ATTRIBUTES, REQUIRED_PRODUCT_ATTRIBUTES

type Category = str  # Checked by the versioned taxonomy registry.
type SaleUnit = Literal["штука", "пачка", "коробка"]
type ConstraintStatus = Literal["compatible", "incompatible", "needs_review"]



def _reject_unsafe_attribute_values(value: object) -> None:
    if not isinstance(value, dict):
        raise PydanticCustomError("attributes_type", "Attributes must be an object")
    for key, v in value.items():
        if not isinstance(key, str):
            raise PydanticCustomError("attributes_key_type", "Attribute keys must be strings")
        if type(v) in (str, int):
            continue
        if isinstance(v, list) and 1 <= len(v) <= 100 and all(
            type(item) is str and 1 <= len(item.strip()) <= 100 for item in v
        ) and len(set(v)) == len(v):
            continue
        raise ValueError("Attributes must be strings, integers or bounded unique string lists")


def _validate_attributes(
    category: Category,
    attributes: dict[str, str | int | list[str]],
    *,
    require_product: bool,
) -> None:
    if category not in CATEGORY_ATTRIBUTES:
        raise ValueError(f"Unsupported category: {category}")
    spec = {item.name: item for item in CATEGORY_ATTRIBUTES[category]}
    unexpected = sorted(set(attributes) - set(spec))
    if unexpected:
        raise ValueError(f"Unsupported attributes: {', '.join(unexpected)}")
    if require_product:
        missing = sorted(REQUIRED_PRODUCT_ATTRIBUTES[category] - attributes.keys())
        if missing:
            raise ValueError(f"Required attributes missing: {', '.join(missing)}")
    for name, value in attributes.items():
        expected = spec[name].value_kind
        if type(value) is not expected:
            raise ValueError(f"Invalid type for {name}: expected {expected.__name__}")
        if expected is list:
            if not isinstance(value, list) or not value:
                raise ValueError(f"Invalid list for {name}")
            continue
        if isinstance(value, str) and (not value.strip() or len(value) > 100):
            raise ValueError(f"Invalid value for {name}")
        if isinstance(value, int) and not 0 < value <= 1_000_000:
            raise ValueError(f"Invalid numeric value for {name}")


class CatalogProduct(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sku: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=500)
    category: Category
    sale_unit: SaleUnit
    pieces_per_sale_unit: int = Field(gt=0, le=100000)
    price_rub: Decimal = Field(ge=Decimal(0), max_digits=14, decimal_places=2)
    brand: str | None = None
    paper_format: str | None = None
    paper_gsm: int | None = Field(default=None, ge=1, le=1000)
    pen_ink_color: str | None = None
    pen_ink_type: str | None = None
    attributes: dict[str, str | int | list[str]] = Field(default_factory=dict)

    @field_validator("attributes", mode="before")
    @classmethod
    def raw_attributes_only(cls, value: object) -> object:
        _reject_unsafe_attribute_values(value)
        return value

    @model_validator(mode="after")
    def validate_profile(self) -> CatalogProduct:
        _validate_attributes(self.category, self.attributes, require_product=True)
        return self

    @field_validator("sku")
    @classmethod
    def sku_not_only_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("SKU must not be whitespace")
        return value


class RequestedItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str = Field(min_length=1, max_length=2000)
    category: Category
    quantity: int | None = Field(default=None, gt=0, le=100000, strict=True)
    unit: SaleUnit | None = None
    brand: str | None = None
    paper_format: str | None = None
    paper_gsm: int | None = Field(default=None, ge=1, le=1000)
    pen_ink_color: str | None = None
    pen_ink_type: str | None = None
    attributes: dict[str, str | int | list[str]] = Field(default_factory=dict)

    @field_validator("attributes", mode="before")
    @classmethod
    def raw_attributes_only(cls, value: object) -> object:
        _reject_unsafe_attribute_values(value)
        return value

    @model_validator(mode="after")
    def validate_profile(self) -> RequestedItem:
        _validate_attributes(self.category, self.attributes, require_product=False)
        return self


@dataclass(frozen=True, slots=True)
class ConstraintResult:
    status: ConstraintStatus
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Candidate:
    product: CatalogProduct
    constraints: ConstraintResult
    score: float


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    query_id: str
    relevant_skus: frozenset[str]


@dataclass(frozen=True, slots=True)
class RankingMetrics:
    total_queries: int
    positive_queries: int
    no_match_queries: int
    retrieval_hits: int
    hit_at_1: int
    hit_at_3: int
    unretrievable_positive_queries: int
    false_suggestions_on_no_match: int
    correct_empty_no_match: int
