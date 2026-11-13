"""Strict human-reviewed SKU corrections for subsequent supervised ML training.

This module only curates evidence. It never retrains during inference, never
accepts a model's own guess as a label, and does not expose an unauthenticated
endpoint for claiming a human review.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kpdesk.matching.constraints import check_constraints
from kpdesk.matching.schema import CatalogProduct, RequestedItem
from kpdesk.matching.training import LabeledQuery


class MatchFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: uuid.UUID
    catalog_version_id: uuid.UUID
    query_id: str = Field(min_length=1, max_length=100)
    query_group_id: str = Field(min_length=1, max_length=100)
    request: RequestedItem
    sku: str | None = Field(max_length=100)
    verdict: Literal["relevant", "irrelevant", "no_match"]
    reviewed_by: str = Field(min_length=1, max_length=100)
    reviewed_at: datetime
    source: Literal["human_review"]

    @field_validator("reviewed_by")
    @classmethod
    def reviewer_cannot_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Human reviewer required")
        return value

    @field_validator("reviewed_at")
    @classmethod
    def requires_timezone(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("Reviewed timestamp must be timezone aware")
        return value

    @model_validator(mode="after")
    def enforce_label_semantics(self) -> MatchFeedback:
        if self.verdict == "no_match" and self.sku is not None:
            raise ValueError("No-match cannot carry a SKU")
        if self.verdict != "no_match" and not self.sku:
            raise ValueError("SKU required for accept/reject")
        return self


@dataclass(frozen=True, slots=True)
class FeedbackBatch:
    labels: tuple[LabeledQuery, ...]
    explicit_negatives: dict[str, frozenset[str]]
    provenance_sha256: str


def curate_feedback(
    events: list[MatchFeedback],
    products: dict[str, CatalogProduct],
    catalog_version_id: uuid.UUID,
) -> FeedbackBatch:
    """Build independent labels only from reviewed events on one catalog version."""
    if not events:
        raise ValueError("Reviewed feedback is empty.")
    unique_ids: set[uuid.UUID] = set()
    grouped: dict[str, list[MatchFeedback]] = {}
    for event in events:
        if event.event_id in unique_ids:
            raise ValueError("Duplicate feedback event")
        unique_ids.add(event.event_id)
        if event.catalog_version_id != catalog_version_id:
            raise ValueError("Catalog version mismatch")
        if event.sku is not None and event.sku not in products:
            raise ValueError("Feedback references unknown SKU")
        grouped.setdefault(event.query_id, []).append(event)

    labels: list[LabeledQuery] = []
    negatives: dict[str, frozenset[str]] = {}
    for query_id, records in sorted(grouped.items()):
        base = records[0]
        if any(
            event.query_group_id != base.query_group_id
            or event.request != base.request
            for event in records[1:]
        ):
            raise ValueError("Feedback query/group conflict")
        decisions: dict[str, str] = {}
        relevant: set[str] = set()
        rejected: set[str] = set()
        no_match = False
        for event in records:
            if event.verdict == "no_match":
                no_match = True
                continue
            if event.sku is None:
                raise ValueError("Feedback SKU missing for product verdict")
            prior = decisions.get(event.sku)
            if prior is not None and prior != event.verdict:
                raise ValueError("Feedback label conflict for SKU")
            decisions[event.sku] = event.verdict
            if event.verdict == "relevant":
                candidate = products[event.sku]
                if check_constraints(event.request, candidate).status == "incompatible":
                    raise ValueError("Human positive conflicts with hard incompatible product")
                relevant.add(event.sku)
            else:
                rejected.add(event.sku)
        if (not relevant and not no_match) or (no_match and relevant):
            raise ValueError("Feedback lacks a final reviewed decision")
        labels.append(
            LabeledQuery(
                query_id=query_id, query_group_id=base.query_group_id,
                request=base.request, relevant_skus=frozenset(relevant),
            )
        )
        negatives[query_id] = frozenset(rejected)

    canonical = json.dumps(
        [event.model_dump(mode="json") for event in sorted(events, key=lambda e: str(e.event_id))],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return FeedbackBatch(
        labels=tuple(labels),
        explicit_negatives=negatives,
        provenance_sha256=hashlib.sha256(canonical).hexdigest(),
    )
