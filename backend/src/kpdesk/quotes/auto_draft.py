"""Conservative automatic draft assembly. This is NOT automatic approval.

Deterministic compatibility + a unique eligible SKU + lexical evidence permit
draft calculation. Learned ranking cannot bypass hard constraints; an unverified
ML artifact is never used as a self-authorizing confidence threshold.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from kpdesk.matching.constraints import check_constraints
from kpdesk.matching.normalize import normalize_text
from kpdesk.matching.schema import CatalogProduct, RequestedItem
from kpdesk.quotes.calculate import QuoteCalculationError, calculate_line

type AutoLineStatus = Literal["auto_selected", "needs_review", "no_match"]
# Only verifiable identifiers and descriptive equality authorize calculation.
# Retrieval/ranker similarity can recommend for review, never select a SKU.
# Explicit model/size/pack identifiers must not silently change in a draft.
# This is intentionally a conservative review gate, not product recognition.
_EXPLICIT_IDENTIFIER = re.compile(
    r"(?<!\w)[a-zа-яё0-9-]*\d[a-zа-яё0-9-]*(?!\w)", re.IGNORECASE,
)
# Only *explicitly labelled* alphabetic codes are treated as identities.
# Ordinary descriptive words are NOT assumed to be model/SKU codes.
_MARKED_IDENTIFIER = re.compile(
    r"(?<!\w)(?P<kind>модель|артикул|арт\.?|model|oem|sku|p/n|mpn|"
    r"part[ -]?no\.?)(?!\w)\s*(?:№|#|:|=)?\s*"
    r"(?P<code>[a-zа-яё][a-zа-яё0-9._/-]{0,63})(?!\w)",
    re.IGNORECASE,
)


_MARKER_KINDS = {
    "модель": "model", "model": "model",
    "артикул": "article", "арт": "article",
    "oem": "oem", "sku": "supplier_sku",
    "p/n": "manufacturer_part", "mpn": "manufacturer_part",
    "partno": "manufacturer_part", "part-no": "manufacturer_part",
}


def _marked_identity(match: re.Match[str]) -> tuple[str, str]:
    marker = normalize_text(match.group("kind")).rstrip(".").replace(" ", "")
    kind = _MARKER_KINDS[marker]
    code = normalize_text(match.group("code")).rstrip("._-/")
    return kind, code


def _marked_codes(description: str) -> frozenset[tuple[str, str]]:
    # The label is part of identity: model, OEM, part, article and supplier
    # SKU are different domains even when their alphanumeric codes match.
    return frozenset(
        _marked_identity(match)
        for match in _MARKED_IDENTIFIER.finditer(description)
    )


def _verified_description(text: str, *, omit_supplier_sku: bool = False) -> str:
    """Canonicalize only evidenced notational aliases, never arbitrary words.

    Unlike a fuzzy score, this equality does not permit colour/material/
    pack/model substitutions, word omissions or implicit OEM aliases.
    """
    def label(match: re.Match[str]) -> str:
        kind, code = _marked_identity(match)
        if omit_supplier_sku and kind == "supplier_sku":
            return ""
        return f"{kind}={code}"

    marked = _MARKED_IDENTIFIER.sub(label, normalize_text(text))

    def format_code(match: re.Match[str]) -> str:
        token = match.group()
        # A4/A5 Cyrillic and Latin "A" are established paper format aliases.
        if token.startswith("а") and token[1:].isdigit():
            return "a" + token[1:]
        return token

    return normalize_text(_EXPLICIT_IDENTIFIER.sub(format_code, marked))


def _verified_product_identity(
    request: RequestedItem, product: CatalogProduct,
) -> bool:
    wanted = _verified_description(request.description)
    offered = _verified_description(product.name)
    if wanted == offered:
        return True
    # A request may contain an explicitly *supplier* SKU that is absent
    # from the human-readable title. The remaining description must
    # match exactly, so the SKU cannot justify unrelated product wording.
    return (
        _marked_codes(request.description) ==
        frozenset({("supplier_sku", normalize_text(product.sku))})
        and not _marked_codes(product.name)
        and _verified_description(
            request.description, omit_supplier_sku=True,
        ) == offered
    )


def _marked_identity_conflict(
    request: RequestedItem, product: CatalogProduct,
) -> bool:
    """Fail closed on labelled model, OEM, part, article or supplier SKU.

    Exact catalog SKU is evidence only for an explicitly requested supplier
    SKU, *never* proof that an OEM/MPN/model/article has that identity.
    """
    wanted = _marked_codes(request.description)
    offered = _marked_codes(product.name)
    if not wanted and not offered:
        return False
    if offered:
        return wanted != offered
    return wanted != frozenset({("supplier_sku", normalize_text(product.sku))})


def _explicit_identifiers(text: str) -> frozenset[str]:
    normalized = normalize_text(text)
    identifiers = _EXPLICIT_IDENTIFIER.findall(normalized)
    return frozenset(
        ("a" + code[1:] if code.startswith("а") and code[1:].isdigit() else code)
        for code in identifiers
    )


@dataclass(frozen=True, slots=True)
class AutoDraftLine:
    line_number: int
    status: AutoLineStatus
    sku: str | None
    line_total_rub: Decimal | None
    reason_codes: tuple[str, ...]
    selection_method: str | None = None


@dataclass(frozen=True, slots=True)
class AutomaticDraft:
    lines: tuple[AutoDraftLine, ...]
    ready_for_draft: bool
    total_rub: Decimal | None
    approved: bool = False


def build_auto_draft(
    requests: Sequence[RequestedItem | None],
    catalog: list[CatalogProduct],
) -> AutomaticDraft:
    """Return a priced draft only for fully resolved lines; never approve or send."""
    if not requests or len(requests) > 20:
        raise ValueError("A draft requires 1–20 request lines.")
    if len(catalog) > 5000:
        raise ValueError("Catalog exceeds 5000 SKU limit.")
    if len({p.sku for p in catalog}) != len(catalog):
        raise ValueError("Duplicate catalog SKU.")
    results: list[AutoDraftLine] = []
    for number, request in enumerate(requests, start=1):
        if request is None:
            results.append(AutoDraftLine(
                number, "needs_review", None, None, ("category_needs_review",),
            ))
            continue
        candidates = [
            (p, check_constraints(request, p))
            for p in catalog if p.category == request.category
        ]
        compatible = [p for p, rule in candidates if rule.status == "compatible"]
        review = [p for p, rule in candidates if rule.status == "needs_review"]

        def unresolved(
            status: AutoLineStatus, *reasons: str, line_number: int = number,
        ) -> None:
            results.append(AutoDraftLine(line_number, status, None, None, tuple(reasons)))

        if not candidates or (not compatible and not review):
            unresolved("no_match", "no_compatible_sku")
            continue
        if request.category == "stationery_other":
            unresolved("needs_review", "generic_category_requires_review")
            continue
        if review and not compatible:
            unresolved("needs_review", "attributes_not_verified")
            continue
        if len(compatible) != 1 or review:
            unresolved("needs_review", "multiple_compatible_skus")
            continue
        if request.quantity is None or request.unit is None:
            unresolved("needs_review", "missing_quantity_or_unit")
            continue
        selected = compatible[0]
        # Codes in the *descriptive* part must agree. A separately labelled
        # supplier SKU is not a product specification and must not be
        # mistaken for a package size or a model number.
        requested_description = _MARKED_IDENTIFIER.sub("", request.description)
        product_description = _MARKED_IDENTIFIER.sub("", selected.name)
        if (
            _explicit_identifiers(requested_description)
            != _explicit_identifiers(product_description)
            or _marked_identity_conflict(request, selected)
        ):
            unresolved("needs_review", "explicit_identifier_mismatch")
            continue
        # Fuzzy rank is a recommendation, never proof of product identity.
        if not _verified_product_identity(request, selected):
            unresolved("needs_review", "unverified_product_identity")
            continue
        try:
            calculation = calculate_line(request, selected)
        except QuoteCalculationError as error:
            unresolved("needs_review", str(error))
            continue
        if calculation.status != "ready":
            unresolved("needs_review", *calculation.reason_codes)
            continue
        results.append(AutoDraftLine(
            number, "auto_selected", selected.sku,
            calculation.line_total_rub, (),
            selection_method="unique_deterministic_compatible_sku",
        ))

    ready = len(results) == len(requests) and all(
        line.status == "auto_selected" for line in results
    )
    amount = (
        sum((line.line_total_rub for line in results if line.line_total_rub is not None),
            Decimal("0.00"))
        if ready else None
    )
    return AutomaticDraft(tuple(results), ready, amount)
