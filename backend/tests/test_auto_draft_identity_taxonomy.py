"""Invariant: all 40 categories refuse altered titles when identities are unverified.

The 183 items are A46 synthetic taxonomy examples, NOT real gold labels.
"""
from __future__ import annotations

from collections import Counter

from kpdesk.matching.constraints import check_constraints
from kpdesk.matching.profiles import CATEGORY_ATTRIBUTES
from kpdesk.matching.schema import CatalogProduct, RequestedItem
from kpdesk.quotes.auto_draft import build_auto_draft
from scripts.build_a46_taxonomy_coverage import build_taxonomy_coverage


def _request_for(product: CatalogProduct, description: str) -> RequestedItem:
    attributes = dict(product.attributes)
    if product.category in ("toner_cartridge", "ink_cartridge"):
        models = attributes.get("compatible_models")
        if isinstance(models, list) and models:
            attributes["printer_model"] = models[0]
    return RequestedItem(
        description=description, category=product.category,
        quantity=1, unit=product.sale_unit,
        brand=product.brand,
        paper_format=product.paper_format,
        paper_gsm=product.paper_gsm,
        pen_ink_color=product.pen_ink_color,
        pen_ink_type=product.pen_ink_type,
        attributes=attributes,
    )


def test_all_40_categories_disallow_near_match_auto_draft_without_identity() -> None:
    catalog = tuple(
        CatalogProduct.model_validate(row)
        for row in build_taxonomy_coverage()["catalog"]
    )
    assert len(catalog) == 183
    counts: Counter[str] = Counter()
    baseline_automatic = 0
    for product in catalog:
        original = _request_for(product, product.name)
        if (
            check_constraints(original, product).status == "compatible"
            and build_auto_draft([original], [product]).ready_for_draft
        ):
            baseline_automatic += 1
        altered = _request_for(product, product.name + " другое исполнение")
        result = build_auto_draft([altered], [product])
        assert result.ready_for_draft is False, (product.category, product.sku)
        assert result.total_rub is None
        assert result.approved is False
        assert result.lines[0].status != "auto_selected"
        assert result.lines[0].sku is None
        counts[product.category] += 1
    assert set(counts) == set(CATEGORY_ATTRIBUTES)
    assert sum(counts.values()) == 183
    assert baseline_automatic >= 10


def test_identical_normalized_text_remains_eligible_when_constraints_are_met() -> None:
    catalog = tuple(
        CatalogProduct.model_validate(row)
        for row in build_taxonomy_coverage()["catalog"]
    )
    verified = 0
    for product in catalog:
        original = _request_for(product, product.name)
        baseline = build_auto_draft([original], [product])
        if baseline.ready_for_draft:
            normalized = _request_for(product, "   " + product.name.upper() + "   ")
            followup = build_auto_draft([normalized], [product])
            assert followup.ready_for_draft is True, product.sku
            assert followup.lines[0].sku == product.sku
            assert followup.total_rub == baseline.total_rub
            verified += 1
    assert verified >= 10
