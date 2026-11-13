"""Validate optional LLM column-mapping suggestions against exact source evidence.

A model sees bounded metadata and may suggest *which existing column* corresponds
to a semantic field. It never supplies new SKU, price, category, stock, units or
a publishable catalog; accepted candidates remain UNCONFIRMED until user review.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kpdesk.catalog.supplier_inference import SourceColumn, SourceInspection

_FIELDS = frozenset((
    "name", "price", "sku", "currency", "category",
    "sale_unit", "pieces_per_sale_unit",
))


class ModelColumnProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fields: dict[str, int | str] = Field(min_length=2, max_length=7)
    reasoning: str = Field(min_length=1, max_length=600)


@dataclass(frozen=True, slots=True)
class UnconfirmedModelMapping:
    """Evidence-anchored candidate only, NOT trusted AI automation output."""

    source_sha256: str
    fields: dict[str, SourceColumn]
    review_required: bool = True
    approved: bool = False
    publishable: bool = False


def validate_model_mapping(
    inspection: SourceInspection, payload: dict[str, Any],
) -> UnconfirmedModelMapping:
    """Reject model hallucinations, duplicate mappings and source changes."""
    try:
        proposal = ModelColumnProposal.model_validate(payload)
    except ValidationError as error:
        raise ValueError("LLM mapping does not match the strict schema.") from error
    if proposal.source_sha256 != inspection.source_sha256:
        raise ValueError("LLM mapping belongs to a different source file.")
    if not {"name", "price"} <= proposal.fields.keys():
        raise ValueError("Name and price must be mapped to actual source columns.")
    if not proposal.fields.keys() <= _FIELDS:
        raise ValueError("Unknown canonical field proposed by LLM.")
    values = list(proposal.fields.values())
    if any(type(value) not in (int, str) for value in values) or len(values) != len(
        set(values)
    ):
        raise ValueError("Invalid or duplicate source column mapping.")
    json_format = inspection.source_format == "json"
    if json_format:
        if any(not isinstance(value, str) or value not in inspection.observed_columns
               for value in values):
            raise ValueError("Model references an absent JSON source key.")
    elif any(type(value) is not int or not 0 <= value < len(inspection.observed_columns)
             for value in values):
        raise ValueError("Model references an absent source column.")
    if any(not isinstance(name, str) or len(name) > 50 or re.search(r"[\x00-\x1f]", name)
           for name in proposal.fields):
        raise ValueError("Invalid semantic field name.")
    # No model-supplied numeric product values or semantic guesses can reach
    # canonical import at this boundary.
    return UnconfirmedModelMapping(
        source_sha256=inspection.source_sha256,
        fields=dict(proposal.fields),
    )


def model_mapping_metadata(inspection: SourceInspection) -> dict[str, object]:
    """Small source-derived proposal context, no raw products or customer PII.

    The caller is responsible for external-provider consent and cost controls.
    This function makes NO HTTP request.
    """
    return {
        "source_sha256": inspection.source_sha256,
        "format": inspection.source_format,
        "columns": tuple(inspection.observed_columns[:64]),
        "header_row": inspection.header_row,
        "issues": inspection.issues,
        "target_fields": tuple(sorted(_FIELDS)),
        "instruction": (
            "Return only source_sha256, fields (actual observed column names/indices) "
            "and reasoning. Missing facts must not be invented. "
            "This proposal needs human review before import."
        ),
    }
