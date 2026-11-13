"""Extract facts from untrusted LLM text; missing properties remain unknown."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from kpdesk.matching.profiles import CATEGORY_ATTRIBUTES
from kpdesk.matching.schema import Category, SaleUnit

# Provider interpretation/cache discriminator; the public DTO stays a mapping.
EXTRACTION_PROMPT_VERSION = "kpdesk_request_v13"

_PROFILE_FIELDS = {
    rule.name for category in CATEGORY_ATTRIBUTES.values() for rule in category
}


class ExtractedLine(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_quote: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1, max_length=500)
    category: Category | None
    quantity: int | None = Field(ge=1, le=100000)
    unit: SaleUnit | None
    brand: str | None = Field(max_length=150)
    paper_format: str | None = Field(max_length=30)
    paper_gsm: int | None = Field(ge=1, le=1000)
    pen_ink_color: str | None = Field(max_length=100)
    pen_ink_type: str | None = Field(max_length=100)
    attributes: dict[str, str | int | list[str] | None] = Field(default_factory=dict)

    @field_validator("attributes", mode="before")
    @classmethod
    def validate_raw_attributes(cls, value: object) -> object:
        if not isinstance(value, dict):
            raise PydanticCustomError("attributes_type", "attributes must be an object")
        for key, actual in value.items():
            if not isinstance(key, str) or key not in _PROFILE_FIELDS:
                raise ValueError(f"unsupported extracted attribute: {key}")
            if type(actual) not in (str, int, type(None)) and not (
                isinstance(actual, list) and 1 <= len(actual) <= 100 and all(
                    type(entry) is str and 1 <= len(entry.strip()) <= 100
                    for entry in actual
                ) and len(set(actual)) == len(actual)
            ):
                raise ValueError(f"invalid extracted attribute: {key}")
        return value

    @model_validator(mode="after")
    def validate_for_category(self) -> ExtractedLine:
        if self.category is not None and self.category not in CATEGORY_ATTRIBUTES:
            raise ValueError(f"Unsupported category: {self.category}")
        allowed = {
            rule.name: rule.value_kind
            for rule in CATEGORY_ATTRIBUTES[self.category]
        } if self.category is not None else {}
        for key, actual in self.attributes.items():
            if actual is None:
                continue
            expected = allowed.get(key)
            if expected is None:
                raise ValueError(f"{key} is not allowed for this category")
            if type(actual) is not expected:
                raise ValueError(f"wrong value type for {key}")
            if isinstance(actual, int) and not 0 < actual <= 1_000_000:
                raise ValueError(f"invalid positive value for {key}")
            if isinstance(actual, str) and (not actual.strip() or len(actual) > 100):
                raise ValueError(f"invalid text value for {key}")
        if self.category not in ("paper", None) and (
            self.paper_format is not None or self.paper_gsm is not None
        ):
            raise ValueError("paper attributes on non-paper category")
        if self.category not in ("pen", None) and (
            self.pen_ink_color is not None or self.pen_ink_type is not None
        ):
            raise ValueError("pen attributes on non-pen category")
        return self


class ExtractedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lines: list[ExtractedLine] = Field(max_length=20)
    instruction_quotes: list[str] = Field(
        default_factory=list, max_length=10, exclude_if=lambda value: not value,
    )

    @field_validator("instruction_quotes")
    @classmethod
    def bounded_instruction_evidence(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values) or sum(map(len, values)) > 2000:
            raise ValueError("Invalid or oversized instruction evidence.")
        return values
