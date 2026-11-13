"""Commercial document content, separate from deterministic catalog prices."""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kpdesk.company.requisites import PartyDetails
from kpdesk.company.schema import CompanyProfileSnapshot
from kpdesk.quotes.appearance import DocumentAppearance
from kpdesk.quotes.line import LockedQuoteLine, ManagerSkuSelection


class ProposalNarrative(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    headline: str = Field(min_length=1, max_length=160)
    opening: str = Field(min_length=1, max_length=1500)
    solution: str = Field(min_length=1, max_length=4000)
    closing: str = Field(min_length=1, max_length=1000)

    @field_validator("headline", "opening", "solution", "closing")
    @classmethod
    def substantive_text_without_prices(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("Proposal text must not be empty or contain NUL.")
        if re.search(r"₽|(?<!\w)руб(?:\.|лей|ля|ль)?(?!\w)", value, re.IGNORECASE):
            raise ValueError("Prices belong to the deterministic product table.")
        return value.strip()


class ProposalContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    issuer_name: str = Field(min_length=1, max_length=200)
    recipient_name: str = Field(default="", max_length=200)
    manager_brief: str = Field(min_length=1, max_length=2000)
    commercial_terms: str = Field(default="", max_length=2000)

    @field_validator("issuer_name", "manager_brief")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("Document context must not be blank.")
        return value.strip()


class AlternativeSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["relevance", "lower_cost"]
    selections: tuple[ManagerSkuSelection, ...] = Field(min_length=1, max_length=20)


class DocumentAlternative(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    selection: AlternativeSelection
    catalog_version_id: uuid.UUID
    lines: tuple[LockedQuoteLine, ...] = Field(min_length=1, max_length=20)
    total_rub: Decimal = Field(ge=0, max_digits=14, decimal_places=2)


class CommercialDocument(ProposalContext):
    issued_on: date
    narrative: ProposalNarrative
    company_profile: CompanyProfileSnapshot | None = Field(default=None, exclude_if=lambda value: value is None)
    alternatives: tuple[DocumentAlternative, ...] = Field(default=(), max_length=2, exclude_if=lambda value: not value)
    appearance: DocumentAppearance | None = Field(default=None, exclude_if=lambda value: value is None)
    recipient_details: PartyDetails | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def recipient_matches_requisites(self) -> CommercialDocument:
        if self.recipient_details and self.recipient_name not in (
            self.recipient_details.short_name, self.recipient_details.full_name,
        ):
            raise ValueError("recipient_requisites_mismatch")
        return self


class DocumentApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: uuid.UUID
    approved_at: datetime
    source_revision: int = Field(ge=1)
    source_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
