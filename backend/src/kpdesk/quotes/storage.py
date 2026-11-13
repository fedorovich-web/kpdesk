"""Immutable, version-locked PostgreSQL draft revisions with transactional idempotency.

This is a backend-only service until local user authentication is implemented.
All prices and SKUs are obtained from the catalog while holding the active lock.
No quote approval, PDF publication or external sending occurs here.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from kpdesk.catalog.storage import list_catalog_items
from kpdesk.db.models import (
    CatalogActive,
    QuoteDocument,
    QuoteRevision,
    QuoteWriteRequest,
)
from kpdesk.extraction.analysis import product_requirements
from kpdesk.extraction.schema import ExtractedRequest
from kpdesk.extraction.to_matching import ExtractionNeedsReview, to_requested_item
from kpdesk.matching.schema import CatalogProduct, RequestedItem
from kpdesk.quotes.auto_draft import build_auto_draft
from kpdesk.quotes.calculate import MAX_RUB, QuoteCalculationError, calculate_line
from kpdesk.quotes.document import CommercialDocument, DocumentApproval
from kpdesk.quotes.instructions import (
    ConfirmedInstructions,
    brand_allowed,
    check_confirmation,
    filter_catalog_by_brand,
    selling_total,
)
from kpdesk.quotes.line import (
    LockedQuoteLine as LockedQuoteLine,  # noqa: PLC0414 - public compatibility export
)
from kpdesk.quotes.line import (
    ManagerSkuSelection as ManagerSkuSelection,  # noqa: PLC0414 - public compatibility export
)


class QuoteConflict(ValueError):
    """Stable codes: stale_revision, stale_catalog, idempotency_conflict."""


class QuoteNotFound(LookupError):
    pass


class QuoteSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ready_for_draft: bool
    total_rub: Decimal | None
    lines: tuple[LockedQuoteLine, ...] = Field(min_length=1, max_length=20)
    source_request: ExtractedRequest | None = None
    instructions: ConfirmedInstructions | None = None
    base_total_rub: Decimal | None = None
    estimated_total_rub: Decimal | None = None
    budget_exceeded: bool = False
    approved: bool = False
    document: CommercialDocument | None = None
    approval: DocumentApproval | None = None

    @model_validator(mode="after")
    def valid_document_approval(self) -> QuoteSnapshot:
        if self.approved and (
            not self.ready_for_draft or self.total_rub is None
            or self.document is None or self.approval is None
        ):
            raise ValueError("incomplete_commercial_document")
        if not self.approved and self.approval is not None:
            raise ValueError("unexpected_document_approval")
        return self


@dataclass(frozen=True, slots=True)
class StoredQuote:
    quote_id: uuid.UUID
    revision: int
    catalog_version_id: uuid.UUID
    ready_for_draft: bool
    total_rub: Decimal | None
    lines: tuple[LockedQuoteLine, ...]
    snapshot_sha256: str
    approved: bool = False
    source_request: ExtractedRequest | None = None
    instructions: ConfirmedInstructions | None = None
    base_total_rub: Decimal | None = None
    estimated_total_rub: Decimal | None = None
    budget_exceeded: bool = False
    document: CommercialDocument | None = None
    approval: DocumentApproval | None = None


def _digest(payload: object) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _hash_request(
    catalog_version_id: str | uuid.UUID,
    extracted: ExtractedRequest,
    quote_id: uuid.UUID | None,
    expected_revision: int | None,
    selections: list[ManagerSkuSelection] | None = None,
    instructions: ConfirmedInstructions | None = None,
) -> str:
    payload: dict[str, object] = {
        "catalog_version_id": str(catalog_version_id),
        "extracted_request": extracted.model_dump(mode="json"),
        "quote_id": str(quote_id) if quote_id else None,
        "expected_revision": expected_revision,
    }
    if selections is not None:
        payload["manager_selections"] = [
            selection.model_dump(mode="json")
            for selection in sorted(selections, key=lambda item: item.line_number)
        ]
    if instructions is not None:
        payload["instructions"] = instructions.model_dump(mode="json")
    return _digest(payload)


def _make_snapshot(
    extracted: ExtractedRequest,
    products: list[CatalogProduct],
    selections: list[ManagerSkuSelection] | None = None,
    instructions: ConfirmedInstructions | None = None,
) -> QuoteSnapshot:
    rules = check_confirmation(instructions) if instructions is not None else None
    if not extracted.lines:
        raise ValueError("No extracted lines")
    requested: list[RequestedItem | None] = []
    for item in extracted.lines:
        try:
            requested.append(to_requested_item(item))
        except ExtractionNeedsReview:
            requested.append(None)
    chosen = {selection.line_number: selection for selection in selections or []}
    if len(chosen) != len(selections or []) or any(
        line_number > len(extracted.lines) for line_number in chosen
    ):
        raise ValueError("Invalid manager selection line identity.")
    eligible = filter_catalog_by_brand(products, rules)
    result = build_auto_draft(requested, eligible)
    indexed = {p.sku: p for p in products}
    eligible_index = {p.sku: p for p in eligible}
    lines: list[LockedQuoteLine] = []
    for auto, source, request in zip(result.lines, extracted.lines, requested, strict=True):
        chosen_item = chosen.get(auto.line_number)
        if chosen_item is not None:
            if request is None:
                raise ValueError("Manager selection requires a classified request.")
            selected = indexed.get(chosen_item.sku)
            if selected is None:
                raise ValueError("Selected SKU is missing from the current catalog.")
            if rules is not None and not brand_allowed(selected.brand, rules):
                raise ValueError("manager_selection_violates_brand_policy")
            try:
                calculation = calculate_line(
                    request, selected, chosen_item.approved_sale_quantity,
                )
            except QuoteCalculationError as error:
                raise ValueError(f"Invalid manager selection: {error}") from error
            if calculation.status != "ready" or calculation.line_total_rub is None:
                raise ValueError("Selected SKU has unresolved price or packaging.")
            lines.append(LockedQuoteLine(
                line_number=auto.line_number,
                description=source.description,
                status="manager_selected",
                sku=selected.sku,
                product_name=selected.name,
                requested_quantity=request.quantity,
                requested_unit=request.unit,
                sale_quantity=calculation.sale_quantity,
                sale_unit=calculation.sale_unit,
                unit_price_rub=calculation.price_rub,
                line_total_rub=calculation.line_total_rub,
                reason_codes=calculation.reason_codes,
                product_brand=selected.brand,
                requirements=product_requirements(request, selected, chosen_item.approved_sale_quantity),
            ))
            continue
        sku = auto.sku
        product = eligible_index.get(sku) if sku is not None else None
        sale_quantity: int | None = None
        sale_unit: str | None = None
        unit_price: Decimal | None = None
        if product is not None and request is not None:
            try:
                calculated = calculate_line(request, product)
            except QuoteCalculationError as error:
                raise ValueError("Automatic draft inconsistent with catalog") from error
            if (calculated.status != "ready"
                or calculated.line_total_rub != auto.line_total_rub):
                raise ValueError("Automatic draft calculation mismatch")
            sale_quantity = calculated.sale_quantity
            sale_unit = calculated.sale_unit
            unit_price = calculated.price_rub
        brand_excluded = (
            rules is not None and request is not None
            and auto.status == "no_match"
            and any(p.category == request.category for p in products)
            and not any(p.category == request.category for p in eligible)
        )
        lines.append(LockedQuoteLine(
            line_number=auto.line_number,
            description=source.description,
            status=auto.status,
            sku=sku,
            product_name=product.name if product else None,
            requested_quantity=source.quantity,
            requested_unit=source.unit,
            sale_quantity=sale_quantity,
            sale_unit=sale_unit,
            unit_price_rub=unit_price,
            line_total_rub=auto.line_total_rub,
            reason_codes=("brand_policy_excluded",) if brand_excluded else auto.reason_codes,
            product_brand=product.brand if product is not None else None,
            requirements=product_requirements(request, product)
            if request is not None and product is not None else (),
        ))
    ready = all(line.status in ("auto_selected", "manager_selected") for line in lines)
    base_total = (
        sum((line.line_total_rub for line in lines if line.line_total_rub is not None),
            Decimal("0.00"))
        if ready else None
    )
    if base_total is not None and base_total > MAX_RUB:
        raise ValueError("Quote amount exceeds supported monetary range.")
    markup = rules.markup_percent if rules else Decimal(0)
    priced = [
        line.model_copy(update={
            "line_total_rub": selling_total(line.line_total_rub, markup),
        }) if line.line_total_rub is not None else line
        for line in lines
    ]
    total = (
        sum((line.line_total_rub for line in priced if line.line_total_rub is not None),
            Decimal("0.00"))
        if ready else None
    )
    if total is not None and total > MAX_RUB:
        raise ValueError("Quote amount exceeds supported monetary range.")
    exceeded = bool(total is not None and rules and rules.budget_max_rub is not None
                    and total > rules.budget_max_rub)
    return QuoteSnapshot(
        ready_for_draft=ready and not exceeded,
        total_rub=total if not exceeded else None,
        estimated_total_rub=total if exceeded else None,
        lines=tuple(priced),
        source_request=extracted,
        instructions=instructions,
        base_total_rub=base_total,
        budget_exceeded=exceeded,
    )


def _from_row(revision: QuoteRevision) -> StoredQuote:
    expected = _digest(revision.snapshot)
    if revision.snapshot_sha256 != expected:
        raise RuntimeError("Quote snapshot hash mismatch")
    content = QuoteSnapshot.model_validate(revision.snapshot)
    if (content.total_rub != revision.total_rub
        or content.ready_for_draft != revision.is_complete):
        raise RuntimeError("Quote amount is inconsistent")
    return StoredQuote(
        quote_id=revision.quote_id,
        revision=revision.number,
        catalog_version_id=revision.catalog_version_id,
        ready_for_draft=content.ready_for_draft,
        total_rub=content.total_rub,
        lines=content.lines,
        snapshot_sha256=revision.snapshot_sha256,
        source_request=content.source_request,
        instructions=content.instructions,
        base_total_rub=content.base_total_rub,
        estimated_total_rub=content.estimated_total_rub,
        budget_exceeded=content.budget_exceeded,
        approved=content.approved,
        document=content.document,
        approval=content.approval,
    )


def _get_revision(
    session: Session, quote_id: uuid.UUID, number: int,
) -> StoredQuote:
    revision = session.get(QuoteRevision, (quote_id, number))
    if revision is None:
        raise QuoteNotFound("quote_revision_not_found")
    return _from_row(revision)


def get_quote_draft(
    session: Session, quote_id: uuid.UUID, revision: int | None = None,
) -> StoredQuote:
    document = session.get(QuoteDocument, quote_id)
    if document is None:
        raise QuoteNotFound("quote_not_found")
    if revision is not None and (revision < 1 or revision > document.current_revision):
        raise QuoteNotFound("quote_revision_not_found")
    return _get_revision(session, quote_id, revision or document.current_revision)


def _existing_or_conflict(
    session: Session, key: str, request_sha256: str,
) -> StoredQuote | None:
    found = session.get(QuoteWriteRequest, key)
    if found is None:
        return None
    if found.request_sha256 != request_sha256:
        raise QuoteConflict("idempotency_conflict")
    return _get_revision(session, found.quote_id, found.revision_number)


def _write_revision(
    session: Session,
    *,
    quote_id: uuid.UUID,
    number: int,
    catalog_version_id: uuid.UUID,
    data: QuoteSnapshot,
    key: str,
    request_sha256: str,
) -> StoredQuote:
    payload = data.model_dump(mode="json")
    row = QuoteRevision(
        quote_id=quote_id, number=number, catalog_version_id=catalog_version_id,
        snapshot=payload, snapshot_sha256=_digest(payload),
        is_complete=data.ready_for_draft, total_rub=data.total_rub,
    )
    session.add(row)
    session.flush()
    session.add(QuoteWriteRequest(
        key=key, request_sha256=request_sha256,
        quote_id=quote_id, revision_number=number,
    ))
    session.flush()
    return _from_row(row)


def _validate_key(key: str) -> None:
    if not key.strip() or len(key) > 128 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ValueError("Invalid Idempotency-Key")


def _locked_active(session: Session, catalog_version_id: uuid.UUID) -> None:
    active = session.get(CatalogActive, 1, with_for_update=True)
    if active is None or active.catalog_version_id != catalog_version_id:
        raise QuoteConflict("stale_catalog")


def create_quote_draft(
    session: Session, catalog_version_id: str | uuid.UUID,
    extracted: ExtractedRequest, key: str,
) -> StoredQuote:
    _validate_key(key)
    version_id = uuid.UUID(str(catalog_version_id))
    request_hash = _hash_request(version_id, extracted, None, None)
    with session.begin():
        _locked_active(session, version_id)  # serialize idempotency across local workspace
        existing = _existing_or_conflict(session, key, request_hash)
        if existing is not None:
            return existing
        snapshot = _make_snapshot(extracted, list_catalog_items(session, version_id))
        document = QuoteDocument(current_revision=1)
        session.add(document)
        session.flush()
        return _write_revision(
            session, quote_id=document.id, number=1,
            catalog_version_id=version_id, data=snapshot, key=key,
            request_sha256=request_hash,
        )


def revise_quote_draft(
    session: Session, quote_id: uuid.UUID, expected_revision: int,
    catalog_version_id: str | uuid.UUID,
    extracted: ExtractedRequest, key: str,
) -> StoredQuote:
    _validate_key(key)
    version_id = uuid.UUID(str(catalog_version_id))
    request_hash = _hash_request(version_id, extracted, quote_id, expected_revision)
    with session.begin():
        # Serialize conflicting edits and key re-use.
        _locked_active(session, version_id)
        existing = _existing_or_conflict(session, key, request_hash)
        if existing is not None:
            return existing
        document = session.get(QuoteDocument, quote_id, with_for_update=True)
        if document is None:
            raise QuoteNotFound("quote_not_found")
        if expected_revision != document.current_revision:
            raise QuoteConflict("stale_revision")
        snapshot = _make_snapshot(extracted, list_catalog_items(session, version_id))
        document.current_revision += 1
        return _write_revision(
            session, quote_id=quote_id, number=document.current_revision,
            catalog_version_id=version_id, data=snapshot, key=key,
            request_sha256=request_hash,
        )
