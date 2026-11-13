"""Explicit, tenant-scoped manager labels; unsigned export, never independent gold."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from kpdesk.extraction.analysis import analyze_request, product_requirements
from kpdesk.extraction.to_matching import to_requested_item
from kpdesk.matching.constraints import check_constraints
from kpdesk.matching.feedback import MatchFeedback, curate_feedback
from kpdesk.matching.normalize import normalize_text
from kpdesk.matching.retrieve import retrieve_candidates
from kpdesk.matching.schema import CatalogProduct, RequestedItem
from kpdesk.quotes.storage import StoredQuote

MAX_FEEDBACK_BYTES = 10 * 1024 * 1024
type Sku = Annotated[str, Field(min_length=1, max_length=100)]


class FeedbackScope(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    expected_revision: int = Field(ge=1, strict=True)
    source_snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class FeedbackDecision(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    line_number: int = Field(ge=1, le=20, strict=True)
    relevant_sku: Sku | None
    rejected_skus: tuple[Sku, ...] = Field(min_length=1, max_length=20)
    no_match: StrictBool

    @model_validator(mode='after')
    def explicit_decision(self) -> FeedbackDecision:
        if (self.no_match != (self.relevant_sku is None)
                or self.relevant_sku in self.rejected_skus
                or len(set(self.rejected_skus)) != len(self.rejected_skus)
                or any(not sku.strip() for sku in (*self.rejected_skus, *((self.relevant_sku,) if self.relevant_sku else ())))):
            raise ValueError('feedback_decision_conflict')
        return self


class FeedbackExportRequest(FeedbackScope):
    decisions: tuple[FeedbackDecision, ...] = Field(min_length=1, max_length=20)
    reviewed: StrictBool
    training_consent: StrictBool


def catalog_digest(products: tuple[CatalogProduct, ...]) -> str:
    raw = json.dumps([p.model_dump(mode='json') for p in products], ensure_ascii=False,
                     sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


class FeedbackBundle(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    format: Literal['kpdesk-manager-feedback-v1'] = 'kpdesk-manager-feedback-v1'
    training_data_kind: Literal['manager_reviewed'] = 'manager_reviewed'
    organization_id: UUID
    quote_id: UUID
    revision: int = Field(ge=1, strict=True)
    snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    catalog_version_id: UUID
    reviewer_id: UUID
    reviewed_at: datetime
    training_consent: StrictBool
    catalog_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    catalog: tuple[CatalogProduct, ...] = Field(min_length=1, max_length=5000)
    events: tuple[MatchFeedback, ...] = Field(min_length=1, max_length=420)

    @model_validator(mode='after')
    def consistent_scope(self) -> FeedbackBundle:
        if (self.reviewed_at.utcoffset() is None or not self.training_consent
                or any(p.price_rub != 0 for p in self.catalog)
                or len({p.sku for p in self.catalog}) != len(self.catalog)
                or self.catalog_sha256 != catalog_digest(self.catalog)
                or any(e.catalog_version_id != self.catalog_version_id
                       or e.reviewed_by != str(self.reviewer_id) or e.reviewed_at != self.reviewed_at
                       for e in self.events)):
            raise ValueError('feedback_bundle_scope_mismatch')
        return self


def _requests(quote: StoredQuote, products: list[CatalogProduct]) -> list[RequestedItem]:
    if not quote.source_request or not quote.source_request.lines:
        raise ValueError('feedback_source_request_required')
    if any(line.supplier_id or line.source_catalog_version_id for line in quote.lines):
        raise ValueError('feedback_single_catalog_required')
    if not products or len(products) > 5000 or len({p.sku for p in products}) != len(products):
        raise ValueError('feedback_catalog_limit')
    if not analyze_request(quote.source_request).ready_for_matching:
        raise ValueError('feedback_complete_request_required')
    numbers = [line.line_number for line in quote.lines]
    if sorted(numbers) != list(range(1, len(quote.source_request.lines) + 1)):
        raise ValueError('feedback_line_mismatch')
    return [to_requested_item(line) for line in quote.source_request.lines]


def feedback_context(quote: StoredQuote, products: list[CatalogProduct]) -> dict[str, Any]:
    requests = _requests(quote, products)
    indexed = {p.sku: p for p in products}
    selected = {line.line_number: line.sku for line in quote.lines}
    rows = []
    for number, request in enumerate(requests, 1):
        pool = (sorted(products, key=lambda p: p.sku) if len(products) <= 20 else
                [c.product for c in retrieve_candidates(request, products, limit=20)])
        sku = selected[number]
        if sku is not None:
            if sku not in indexed:
                raise ValueError('feedback_candidate_invalid')
            if sku not in {p.sku for p in pool}:
                pool = [indexed[sku], *pool[:19]]
        rows.append({'line_number': number, 'description': request.description, 'selected_sku': sku,
                     'no_match_allowed': len(products) <= 20,
                     'candidates': [{'sku': p.sku, 'name': p.name,
                                     'requirements': [c.model_dump(mode='json') for c in product_requirements(request, p)]}
                                    for p in pool]})
    return {'quote_id': quote.quote_id, 'source_revision': quote.revision,
            'source_snapshot_sha256': quote.snapshot_sha256, 'catalog_version_id': quote.catalog_version_id,
            'lines': rows}


def build_feedback_bundle(
    quote: StoredQuote, products: list[CatalogProduct], body: FeedbackExportRequest, *,
    organization_id: UUID, reviewer_id: UUID, reviewed_at: datetime | None = None,
) -> FeedbackBundle:
    if not body.reviewed or not body.training_consent:
        raise ValueError('feedback_review_required')
    if body.expected_revision != quote.revision or body.source_snapshot_sha256 != quote.snapshot_sha256:
        raise ValueError('feedback_snapshot_mismatch')
    requests = _requests(quote, products)
    decisions = {d.line_number: d for d in body.decisions}
    if len(decisions) != len(body.decisions) or set(decisions) != set(range(1, len(requests) + 1)):
        raise ValueError('feedback_line_mismatch')
    indexed = {p.sku: p for p in products}
    now = reviewed_at or datetime.now(UTC)
    events = []
    for number, request in enumerate(requests, 1):
        decision = decisions[number]
        skus = (*decision.rejected_skus, *((decision.relevant_sku,) if decision.relevant_sku else ()))
        if any(sku not in indexed for sku in skus):
            raise ValueError('feedback_candidate_invalid')
        if decision.no_match and set(decision.rejected_skus) != set(indexed):
            raise ValueError('feedback_no_match_requires_full_catalog')
        if decision.relevant_sku and check_constraints(request, indexed[decision.relevant_sku]).status != 'compatible':
            raise ValueError('feedback_positive_incompatible')
        identity = f'{organization_id}:{quote.quote_id}:{quote.revision}:{quote.snapshot_sha256}:{number}'
        query_id = 'q-' + hashlib.sha256(identity.encode()).hexdigest()[:40]
        group = 'g-' + hashlib.sha256(normalize_text(request.description).encode()).hexdigest()[:40]
        labels: list[tuple[str | None, str]] = (
            [(decision.relevant_sku, 'relevant')] if decision.relevant_sku else [(None, 'no_match')])
        labels += [(sku, 'irrelevant') for sku in decision.rejected_skus]
        for sku, verdict in labels:
            events.append(MatchFeedback.model_validate({
                'event_id': uuid4(), 'catalog_version_id': quote.catalog_version_id,
                'query_id': query_id, 'query_group_id': group, 'request': request,
                'sku': sku, 'verdict': verdict, 'reviewed_by': str(reviewer_id),
                'reviewed_at': now, 'source': 'human_review',
            }))
    catalog = tuple(p.model_copy(update={'price_rub': Decimal('0.00')}) for p in sorted(products, key=lambda p: p.sku))
    bundle = FeedbackBundle(organization_id=organization_id, quote_id=quote.quote_id, revision=quote.revision,
                            snapshot_sha256=quote.snapshot_sha256, catalog_version_id=quote.catalog_version_id,
                            reviewer_id=reviewer_id, reviewed_at=now, catalog=catalog,
                            training_consent=body.training_consent,
                            catalog_sha256=catalog_digest(catalog), events=tuple(events))
    if len(bundle.model_dump_json().encode('utf-8')) > MAX_FEEDBACK_BYTES:
        raise ValueError('feedback_too_large')
    return bundle


def dataset_from_feedback(bundles: list[FeedbackBundle]) -> dict[str, Any]:
    if not bundles:
        raise ValueError('feedback_empty')
    first = bundles[0]
    events: dict[UUID, MatchFeedback] = {}
    for bundle in bundles:
        if (bundle.organization_id, bundle.catalog_version_id, bundle.catalog_sha256) != (
            first.organization_id, first.catalog_version_id, first.catalog_sha256,
        ):
            raise ValueError('feedback_dataset_scope_mismatch')
        for event in bundle.events:
            if event.event_id in events and events[event.event_id] != event:
                raise ValueError('feedback_event_conflict')
            events[event.event_id] = event
    products = {p.sku: p for p in first.catalog}
    for event in events.values():
        if event.verdict == 'relevant' and (event.sku not in products or
                check_constraints(event.request, products[event.sku]).status != 'compatible'):
            raise ValueError('feedback_positive_incompatible')
    source = json.dumps([event.model_dump(mode='json') for event in sorted(events.values(), key=lambda e: str(e.event_id))],
                        ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    canonical_events = []
    for event in events.values():
        request = event.request.model_copy(update={'description': normalize_text(event.request.description)})
        fingerprint = json.dumps(request.model_dump(mode='json'), sort_keys=True, separators=(',', ':'))
        query = 'q-' + hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()[:40]
        group = 'g-' + hashlib.sha256(request.description.encode('utf-8')).hexdigest()[:40]
        canonical_events.append(event.model_copy(update={'request': request, 'query_id': query, 'query_group_id': group}))
    curated = curate_feedback(canonical_events, products, first.catalog_version_id)
    if any(not curated.explicit_negatives[label.query_id] for label in curated.labels):
        raise ValueError('feedback_explicit_negatives_required')
    if any(not label.relevant_skus and curated.explicit_negatives[label.query_id] != frozenset(products)
           for label in curated.labels):
        raise ValueError('feedback_no_match_requires_full_catalog')
    return {'provenance': {'kind': 'manager_reviewed', 'annotation_method': 'explicit_owner_feedback_export',
                           'organization_id': str(first.organization_id),
                           'catalog_version_id': str(first.catalog_version_id),
                           'feedback_sha256': hashlib.sha256(source).hexdigest(),
                           'curation_sha256': curated.provenance_sha256,
                           'source_query_count': len({event.query_id for event in events.values()}),
                           'independent_gold': False},
            'catalog': [p.model_dump(mode='json') for p in first.catalog],
            'queries': [{'query_id': label.query_id, 'query_group_id': label.query_group_id,
                         'request': label.request.model_dump(mode='json'), 'relevant_skus': sorted(label.relevant_skus),
                         'reviewed_negative_skus': sorted(curated.explicit_negatives[label.query_id])}
                        for label in curated.labels]}
