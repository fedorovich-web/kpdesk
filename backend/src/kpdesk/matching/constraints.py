"""Deterministic category and device compatibility constraints before ML scoring."""

import re

from kpdesk.matching.attribute_values import normalize_attribute_value
from kpdesk.matching.normalize import normalize_text
from kpdesk.matching.profiles import CATEGORY_ATTRIBUTES, REQUIRED_PRODUCT_ATTRIBUTES
from kpdesk.matching.schema import CatalogProduct, ConstraintResult, RequestedItem

CARTRIDGES = frozenset(("toner_cartridge", "ink_cartridge"))


def description_category_conflict(category: str | None, description: str) -> bool:
    """Refuse observed adjacent-category substitutions before any ranker.

    This is a bounded contradiction check, not automatic recategorization.
    A refill is not a complete pen; self-adhesive labels are not office paper.
    The existing taxonomy has pen_refill/label for explicitly classified input.
    """
    description = normalize_text(description)
    description = re.sub(r'^\s*(?:(?:\d{1,3}[.)]|[-–—•])\s*)+', '', description)
    # A complete source quote may start with its explicit count/unit header.
    # Strip only that bounded prefix for the kind check, never product words.
    description = re.sub(
        r'^(?:(?:нужно|нужны|требуется|требуются)\s+)?\d{1,6}\s*'
        r'(?:шт\.?|штук(?:а|и)?|пачк[аиу]|пачек|коробк[аиу]|коробок)\s*:\s*',
        '', description,
    )
    if category == 'pen':
        return re.match(r'^стерж(?:ень|ни|ней|ня)\b', description) is not None
    if category == 'paper':
        def positive(pattern: str) -> bool:
            return any(not re.search(r'\b(?:не|без)\s*$', description[:match.start()])
                       for match in re.finditer(pattern, description))
        return positive(r'\bсамокле\w*\b') and positive(r'\b(?:этикет\w*|ярлык\w*)\b')
    return False


def _normalize_paper_format(value: str) -> str:
    return normalize_text(value).replace("а", "a").replace(" ", "")


def check_constraints(request: RequestedItem, product: CatalogProduct) -> ConstraintResult:
    if request.category != product.category:
        return ConstraintResult("incompatible", ("category_mismatch",))
    if description_category_conflict(request.category, request.description):
        return ConstraintResult('incompatible', ('category_description_mismatch',))

    incompatible: list[str] = []
    unknown: list[str] = []
    legacy_fields = (
        ("paper_format", "paper_gsm") if request.category == "paper" else
        ("pen_ink_color", "pen_ink_type") if request.category == "pen" else ()
    )
    for name in legacy_fields:
        want = getattr(request, name)
        actual = getattr(product, name)
        if actual is None or (isinstance(actual, str) and not actual.strip()):
            unknown.append(f"{name}_unknown")
            continue
        if want is None:
            unknown.append(f"{name}_request_unknown")
            continue
        if name == "paper_format":
            matches = _normalize_paper_format(str(want)) == _normalize_paper_format(str(actual))
        else:
            matches = normalize_attribute_value(name, want) == normalize_attribute_value(name, actual)
        if not matches:
            incompatible.append(f"{name}_mismatch")

    required = REQUIRED_PRODUCT_ATTRIBUTES[request.category]
    for field in CATEGORY_ATTRIBUTES[request.category]:
        name = field.name
        if request.category in CARTRIDGES and name in ("compatible_models", "printer_model"):
            continue  # Device family is checked against the full explicit allowlist below.
        actual = product.attributes.get(name)
        want = request.attributes.get(name)
        if actual is None:
            if name in required or want is not None:
                unknown.append(f"{name}_unknown")
            continue
        if want is None:
            if name in required and request.category not in CARTRIDGES:
                unknown.append(f"{name}_request_unknown")
            continue
        if field.mode == "minimum":
            matches = isinstance(actual, int) and isinstance(want, int) and actual >= want
        else:
            matches = normalize_attribute_value(name, str(actual)) == normalize_attribute_value(name, str(want))
        if not matches:
            incompatible.append(f"{name}_mismatch")

    if request.category in CARTRIDGES:
        wanted_model = request.attributes.get("printer_model")
        compatible = product.attributes.get("compatible_models")
        if not isinstance(compatible, list) or not compatible:
            unknown.append("compatible_models_unknown")
        elif isinstance(wanted_model, str):
            if normalize_text(wanted_model) not in {
                normalize_text(model) for model in compatible
            }:
                incompatible.append("printer_model_incompatible")
        elif not request.attributes.get("cartridge_code"):
            unknown.append("cartridge_identifier_missing")
        # OEM/compatible is commercially material; never assume interchangeability.
        if not request.attributes.get("oem_status"):
            unknown.append("oem_status_request_unknown")
        if not request.attributes.get("color"):
            unknown.append("color_request_unknown")

    if request.brand is not None:
        if not product.brand:
            unknown.append("brand_unknown")
        elif normalize_text(request.brand) != normalize_text(product.brand):
            incompatible.append("brand_mismatch")

    if incompatible:
        return ConstraintResult("incompatible", tuple(incompatible))
    if unknown:
        return ConstraintResult("needs_review", tuple(unknown))
    return ConstraintResult("compatible")
