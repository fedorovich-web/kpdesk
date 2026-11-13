"""Resolve client document choices against the quote's immutable catalog version."""
from __future__ import annotations

from collections.abc import Sequence

from kpdesk.extraction.to_matching import to_requested_item
from kpdesk.matching.rank import rank_candidates
from kpdesk.matching.retrieve import retrieve_candidates
from kpdesk.matching.schema import CatalogProduct
from kpdesk.quotes.document import AlternativeSelection, DocumentAlternative
from kpdesk.quotes.instructions import QuoteRules, check_confirmation, filter_catalog_by_brand
from kpdesk.quotes.line import LockedQuoteLine, ManagerSkuSelection
from kpdesk.quotes.storage import StoredQuote, _make_snapshot
from kpdesk.quotes.variants import build_variants


def _identity(lines: Sequence[LockedQuoteLine]) -> tuple[tuple[int, str | None, int | None], ...]:
    return tuple((line.line_number, line.sku, line.sale_quantity) for line in lines)


def _check_source(quote: StoredQuote) -> None:
    if not quote.ready_for_draft or quote.total_rub is None or quote.approved:
        raise ValueError("quote_not_ready_for_alternatives")
    if quote.source_request is None:
        raise ValueError("alternative_source_request_unavailable")
    if any(line.supplier_id is not None for line in quote.lines):
        raise ValueError("multi_source_alternatives_unavailable")


def resolve_alternatives(
    quote: StoredQuote, products: list[CatalogProduct], selections: Sequence[AlternativeSelection],
) -> tuple[DocumentAlternative, ...]:
    _check_source(quote)
    if len(selections) > 2:
        raise ValueError("too_many_alternatives")
    assert quote.source_request is not None
    seen = {_identity(quote.lines)}
    result: list[DocumentAlternative] = []
    for selection in selections:
        if [item.line_number for item in selection.selections] != list(range(1, len(quote.lines) + 1)):
            raise ValueError("incomplete_alternative_selection")
        snapshot = _make_snapshot(quote.source_request, products, list(selection.selections), quote.instructions)
        if not snapshot.ready_for_draft or snapshot.total_rub is None:
            raise ValueError("alternative_not_complete_or_over_budget")
        identity = _identity(snapshot.lines)
        if identity in seen:
            raise ValueError("duplicate_composition")
        seen.add(identity)
        result.append(DocumentAlternative(
            selection=selection, catalog_version_id=quote.catalog_version_id,
            lines=snapshot.lines, total_rub=snapshot.total_rub,
        ))
    return tuple(result)


def available_alternatives(quote: StoredQuote, products: list[CatalogProduct]) -> tuple[DocumentAlternative, ...]:
    _check_source(quote)
    assert quote.source_request is not None
    requests = [to_requested_item(line) for line in quote.source_request.lines]
    rules = check_confirmation(quote.instructions) if quote.instructions else QuoteRules()
    eligible = filter_catalog_by_brand(products, rules)
    pools = [rank_candidates(item, retrieve_candidates(item, eligible, limit=20), "tfidf") for item in requests]
    variants = build_variants(requests, pools, rules)
    primary = _identity(quote.lines)
    selections = [AlternativeSelection(kind=variant.kind, selections=tuple(
        ManagerSkuSelection(line_number=line.line_number, sku=line.sku) for line in variant.lines
    )) for variant in variants if variant.selectable and tuple(
        (line.line_number, line.sku, line.sale_quantity) for line in variant.lines
    ) != primary]
    return resolve_alternatives(quote, products, selections)
