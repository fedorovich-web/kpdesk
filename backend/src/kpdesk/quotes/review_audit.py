"""Attest a manager's reviewed LLM preview in the quote transaction.

Hashes bind the source, original provider response, and final revised lines.
No approval is implied: quote PDF stays a draft, never a signed offer.
"""
from __future__ import annotations

import hashlib
import uuid

from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import Connection, bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from kpdesk.extraction.schema import ExtractedRequest
from kpdesk.quotes.instructions import ConfirmedInstructions
from kpdesk.quotes.line import ManagerSkuSelection
from kpdesk.quotes.storage import _digest, _hash_request


class QuoteReviewDenied(ValueError):
    """The attestation cannot be cryptographically linked to this revision."""


class ReviewEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    extraction_id: uuid.UUID
    source_text: str = Field(min_length=1, max_length=12000)
    original_extracted_request: ExtractedRequest
    reviewed_lines: list[StrictBool] = Field(min_length=1, max_length=20)


def sha256_source(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def attest_quote_review(
    connection: Connection, *,
    organization_id: uuid.UUID, quote_id: uuid.UUID, revision: int,
    catalog_id: uuid.UUID, extracted: ExtractedRequest,
    expected_revision: int | None, evidence: ReviewEvidence,
    selections: list[ManagerSkuSelection] | None = None,
    instructions: ConfirmedInstructions | None = None,
) -> None:
    if (
        not evidence.source_text.strip()
        or len(evidence.reviewed_lines) != len(extracted.lines)
        or not all(evidence.reviewed_lines)
        or len(evidence.original_extracted_request.lines) == 0
        or any(
            line.source_quote not in evidence.source_text
            for line in evidence.original_extracted_request.lines
        )
        or any(
            line.source_quote not in evidence.source_text
            for line in extracted.lines
        )
    ):
        raise QuoteReviewDenied("source_or_review_mismatch")
    lines = [_digest(line.model_dump(mode="json")) for line in extracted.lines]
    approved = connection.scalar(text("""
        SELECT public.kpdesk_quote_review_attest(
            :org,:quote,:revision,:extraction,:source,:preview,:request,:lines
        )
    """).bindparams(bindparam("lines", type_=JSONB)), {
        "org": organization_id, "quote": quote_id, "revision": revision,
        "extraction": evidence.extraction_id,
        "source": sha256_source(evidence.source_text),
        "preview": _digest(
            evidence.original_extracted_request.model_dump(mode="json")
        ),
        "request": _hash_request(
            catalog_id, extracted, quote_id if expected_revision is not None else None,
            expected_revision, selections, instructions,
        ),
        "lines": lines,
    })
    if approved is not True:
        raise QuoteReviewDenied("review_attestation_denied")
