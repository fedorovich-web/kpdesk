"""Deterministic constraints, lexical ordering and explicit manager decisions."""
from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Literal, TypedDict
from uuid import UUID

from pydantic import TypeAdapter, ValidationError
from rapidfuzz.fuzz import WRatio

from kpdesk.universal.schema import (
    CatalogSnapshot,
    Quantity,
    QuoteContext,
    QuoteLine,
    QuoteSnapshot,
    RequestLine,
    Requirement,
    Selection,
    Specification,
    UniversalProduct,
    UniversalRequest,
    line_total,
    snapshot_digest,
)
from kpdesk.universal.units import convert, normalized, unit_info

STRATEGY = "lexical_universal_v1"
Status = Literal["compatible", "incompatible", "needs_review"]
_SALE_QUANTITY = TypeAdapter(Quantity)


class Check(TypedDict):
    name: str
    status: Status
    reason: str


class Candidate(TypedDict):
    sku: str
    name: str
    score: float
    status: Status
    checks: list[Check]
    reason_codes: list[str]


class PreviewLine(TypedDict):
    line_number: int
    candidates: list[Candidate]


class MatchPreview(TypedDict):
    strategy: str
    requires_manager_selection: bool
    lines: list[PreviewLine]


def _specifications_by_name(specs: tuple[Specification, ...]) -> dict[str, list[Specification]]:
    indexed: dict[str, list[Specification]] = {}
    for spec in specs:
        indexed.setdefault(normalized(spec.name), []).append(spec)
    return indexed


def _requirement(requirement: Requirement, specs: dict[str, list[Specification]]) -> tuple[Status, str]:
    matches = specs.get(normalized(requirement.name), [])
    if len(matches) != 1:
        return "needs_review", "spec_missing" if not matches else "spec_ambiguous"
    spec = matches[0]
    if isinstance(requirement.value, str):
        if not isinstance(spec.value, str):
            return "needs_review", "spec_type_mismatch"
        if normalized(spec.unit or "") != normalized(requirement.unit or ""):
            return "incompatible", "spec_unit_mismatch"
        passed = normalized(spec.value) == normalized(requirement.value)
    else:
        if isinstance(spec.value, str):
            return "needs_review", "spec_type_mismatch"
        if any(unit is not None and unit_info(unit) is None for unit in (spec.unit, requirement.unit)):
            return "needs_review", "spec_unit_unknown"
        try:
            actual = convert(spec.value, spec.unit, requirement.unit)
        except ValueError:
            return "incompatible", "spec_unit_mismatch"
        passed = {"eq": actual == requirement.value, "min": actual >= requirement.value,
                  "max": actual <= requirement.value}[requirement.operator]
    return ("compatible", "spec_verified") if passed else ("incompatible", "spec_mismatch")


def _quantity(line: RequestLine, product: UniversalProduct) -> tuple[Decimal | None, Status, str]:
    if line.quantity is None:
        return None, "needs_review", "quantity_missing"
    if line.sale_unit is None:
        return None, "needs_review", "unit_missing"
    try:
        quantity = convert(line.quantity, line.sale_unit, product.sale_unit)
    except ValueError:
        return None, "incompatible", "unit_mismatch"
    try:
        quantity = _SALE_QUANTITY.validate_python(quantity)
    except ValidationError:
        return None, "incompatible", "quantity_precision"
    info = unit_info(product.sale_unit)
    if info is not None and info[0] == "count" and quantity != quantity.to_integral_value():
        return None, "incompatible", "fractional_discrete_quantity"
    if product.quantity_step is not None and quantity % product.quantity_step != 0:
        return None, "incompatible", "quantity_step_mismatch"
    return quantity.normalize(), "compatible", "quantity_verified"


def _candidate(line: RequestLine, product: UniversalProduct, specs: dict[str, list[Specification]]) -> Candidate:
    checks: list[Check] = []
    for field in ("sku", "brand", "model"):
        required = getattr(line, field)
        if required is not None:
            actual = getattr(product, field)
            status: Status = ("needs_review" if actual is None else "compatible"
                              if normalized(required) == normalized(actual) else "incompatible")
            checks.append({"name": field, "status": status,
                           "reason": field + ("_verified" if status == "compatible" else
                                               "_missing" if actual is None else "_mismatch")})
    _, quantity_status, quantity_reason = _quantity(line, product)
    checks.append({"name": "quantity", "status": quantity_status, "reason": quantity_reason})
    for requirement in line.requirements:
        status, reason = _requirement(requirement, specs)
        checks.append({"name": requirement.name, "status": status, "reason": reason})
    # Repeated names in the request are ambiguous, even if individually satisfiable.
    names = [normalized(req.name) for req in line.requirements]
    if len(names) != len(set(names)):
        checks.append({"name": "requirements", "status": "needs_review", "reason": "requirement_ambiguous"})
    overall: Status = ("incompatible" if any(c["status"] == "incompatible" for c in checks)
                       else "needs_review" if any(c["status"] == "needs_review" for c in checks)
                       else "compatible")
    return {"sku": product.sku, "name": product.name,
            "score": round(WRatio(normalized(line.description), normalized(product.name)), 2),
            "status": overall, "checks": checks,
            "reason_codes": list(dict.fromkeys(c["reason"] for c in checks if c["status"] != "compatible"))}


def match_request(request: UniversalRequest, catalog: CatalogSnapshot) -> MatchPreview:
    lines: list[PreviewLine] = []
    rank = {"compatible": 0, "needs_review": 1, "incompatible": 2}
    products = [(product, _specifications_by_name(product.specifications)) for product in catalog.products]
    for number, line in enumerate(request.lines, 1):
        candidates = [_candidate(line, product, specs) for product, specs in products]
        candidates.sort(key=lambda candidate: (rank[candidate["status"]], -candidate["score"], candidate["sku"]))
        lines.append({"line_number": number, "candidates": candidates[:20]})
    return {"strategy": STRATEGY, "requires_manager_selection": True, "lines": lines}


def build_quote(
    catalog: CatalogSnapshot, request: UniversalRequest, selections: Sequence[Selection],
    context: QuoteContext, quote_id: UUID, revision: int,
) -> QuoteSnapshot:
    chosen: dict[int, str] = {}
    products = {product.sku: product for product in catalog.products}
    for selection in selections:
        if selection.line_number in chosen or selection.line_number > len(request.lines):
            raise ValueError("invalid_selection_line")
        if selection.sku not in products:
            raise ValueError("selection_sku_missing")
        chosen[selection.line_number] = selection.sku
    lines: list[QuoteLine] = []
    for number, line in enumerate(request.lines, 1):
        sku = chosen.get(number)
        if sku is None:
            lines.append(QuoteLine(line_number=number, description=line.description, status="unselected",
                                   reason_codes=("manager_selection_required",)))
            continue
        product = products[sku]
        candidate = _candidate(line, product, _specifications_by_name(product.specifications))
        quantity, _, _ = _quantity(line, product)
        compatible = candidate["status"] == "compatible"
        total = (line_total(quantity, product.price_rub, product.price_quantity)
                 if compatible and quantity is not None else None)
        lines.append(QuoteLine(
            line_number=number, description=line.description, sku=sku, name=product.name,
            category=product.category, brand=product.brand, model=product.model,
            sale_quantity=quantity, sale_unit=product.sale_unit,
            price_rub=product.price_rub if compatible else None,
            price_quantity=product.price_quantity if compatible else None,
            line_total_rub=total,
            status="manager_selected" if compatible else "incompatible"
            if candidate["status"] == "incompatible" else "needs_review",
            reason_codes=tuple(candidate["reason_codes"]), specifications=product.specifications,
        ))
    ready = all(line.status == "manager_selected" for line in lines)
    payload: dict[str, object] = {
        **context.model_dump(mode="json"), "quote_id": str(quote_id), "revision": revision,
        "catalog_version_id": str(catalog.version_id), "source_sha256": catalog.source_sha256,
        "source_request": request.model_dump(mode="json"),
        "selections": [selection.model_dump(mode="json") for selection in selections],
        "lines": [line.model_dump(mode="json") for line in lines],
        "total_rub": str(sum((line.line_total_rub or Decimal(0) for line in lines), Decimal("0.00"))) if ready else None,
        "ready_for_draft": ready, "approved": False,
    }
    payload["snapshot_sha256"] = snapshot_digest(payload)
    return QuoteSnapshot.model_validate(payload)
