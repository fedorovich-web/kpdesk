"""Shared input and persisted feedback contracts; no automatic training authority."""
from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from kpdesk.universal.schema import Sha256, UniversalRequest


class FeedbackDecision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    line_number: int = Field(ge=1, le=20, strict=True)
    relevant_sku: str | None = Field(max_length=200)
    negative_skus: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(max_length=5000)

    @model_validator(mode='after')
    def actual_decision(self) -> FeedbackDecision:
        if self.relevant_sku is None and not self.negative_skus:
            raise ValueError('feedback_explicit_decision_required')
        return self


class FeedbackContext(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=1, le=2147483646, strict=True)
    source_snapshot_sha256: Sha256
    reviewed: StrictBool
    training_consent: StrictBool


class FeedbackBody(FeedbackContext):
    decisions: list[FeedbackDecision] = Field(min_length=1, max_length=20)


class StoredDecision(FeedbackDecision):
    no_match_asserted: StrictBool

    @model_validator(mode='after')
    def no_positive_no_match(self) -> StoredDecision:
        if len(set(self.negative_skus)) != len(self.negative_skus) or self.relevant_sku in self.negative_skus:
            raise ValueError('feedback_invalid_sku')
        if self.no_match_asserted and self.relevant_sku is not None:
            raise ValueError('feedback_contradictory_no_match')
        return self


class FeedbackSnapshot(FeedbackContext):
    # A separate field type keeps write input from accepting server-derived flags.
    decisions: list[StoredDecision] = Field(min_length=1, max_length=20)
    id: UUID
    quote_id: UUID
    revision: int = Field(ge=1, strict=True)
    catalog_version_id: UUID
    source_sha256: Sha256
    source_request: UniversalRequest
    reviewer_id: UUID
    curation_status: Literal['pending_review']
    training_activated: StrictBool

    @model_validator(mode='after')
    def consented_source(self) -> FeedbackSnapshot:
        if not self.reviewed or not self.training_consent or self.training_activated:
            raise ValueError('feedback_consent_or_activation_invalid')
        if self.expected_revision != self.revision:
            raise ValueError('feedback_revision_mismatch')
        numbers = [decision.line_number for decision in self.decisions]
        if sorted(numbers) != list(range(1, len(self.source_request.lines) + 1)):
            raise ValueError('feedback_line_mismatch')
        return self

    def input_payload(self) -> dict[str, object]:
        result: dict[str, object] = self.model_dump(mode='json', include=set(FeedbackBody.model_fields))
        result['decisions'] = [decision.model_dump(mode='json', exclude={'no_match_asserted'})
                               for decision in self.decisions]
        return result
