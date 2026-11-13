"""Lexical candidate retrieval; no price influence, no gold-SKU injection."""

from collections.abc import Sequence

from rapidfuzz import fuzz

from kpdesk.matching.constraints import check_constraints
from kpdesk.matching.normalize import normalize_text
from kpdesk.matching.schema import Candidate, CatalogProduct, RequestedItem


def retrieve_candidates(
    request: RequestedItem,
    products: Sequence[CatalogProduct],
    limit: int = 20,
) -> list[Candidate]:
    if not 1 <= limit <= 20:
        raise ValueError("limit must be between 1 and 20")

    wanted = normalize_text(request.description)
    candidates: list[Candidate] = []
    for product in products:
        decision = check_constraints(request, product)
        if decision.status == "incompatible":
            continue
        score = fuzz.WRatio(wanted, normalize_text(product.name)) / 100.0
        candidates.append(Candidate(product=product, constraints=decision, score=score))

    candidates.sort(
        key=lambda candidate: (
            candidate.constraints.status != "compatible",
            -candidate.score,
            candidate.product.sku,
        )
    )
    return candidates[:limit]
