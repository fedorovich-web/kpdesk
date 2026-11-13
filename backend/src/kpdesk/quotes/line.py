"""Shared immutable quote line and explicit SKU selection contracts."""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kpdesk.extraction.analysis import RequirementCheck


class ManagerSkuSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    line_number: int = Field(ge=1, le=20)
    sku: str = Field(min_length=1, max_length=100)
    approved_sale_quantity: int | None = Field(default=None, ge=1, le=100000, strict=True)


class LockedQuoteLine(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    line_number: int
    description: str
    status: Literal["auto_selected", "manager_selected", "needs_review", "no_match"]
    sku: str | None
    product_name: str | None
    requested_quantity: int | None
    requested_unit: str | None
    sale_quantity: int | None
    sale_unit: str | None
    unit_price_rub: Decimal | None
    line_total_rub: Decimal | None
    reason_codes: tuple[str, ...]
    product_brand: str | None = None
    requirements: tuple[RequirementCheck, ...] = Field(default=(), exclude_if=lambda value: not value)
    # Missing for legacy single-catalog quote revisions, all three must
    # exist for any newly sourced multi-supplier line.
    supplier_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,120}$")
    source_catalog_version_id: uuid.UUID | None = None
    source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def complete_supplier_evidence(self) -> LockedQuoteLine:
        evidence = (
            self.supplier_id,
            self.source_catalog_version_id,
            self.source_sha256,
        )
        if any(value is not None for value in evidence) and not all(
            value is not None for value in evidence
        ):
            raise ValueError("incomplete_supplier_provenance")
        if self.status not in ("manager_selected", "auto_selected") and any(
            value is not None for value in evidence
        ):
            raise ValueError("unresolved_line_cannot_claim_supplier")
        return self


