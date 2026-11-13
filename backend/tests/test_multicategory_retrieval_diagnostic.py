"""Fail-closed contracts for the synthetic multi-category ranking diagnostic."""
from __future__ import annotations

from pathlib import Path

import pytest

from kpdesk.matching.constraints import check_constraints
from kpdesk.matching.profiles import CATEGORY_ATTRIBUTES
from kpdesk.matching.schema import CatalogProduct
from scripts.audit_multicategory_retrieval import (
    _catalog_at_size,
    _load_frozen_model,
    _negative_cases,
    _positive_cases,
    build_multicategory_retrieval_report,
    main,
)
from scripts.build_a46_taxonomy_coverage import build_taxonomy_coverage


def _source() -> tuple[dict[str, object], tuple[CatalogProduct, ...]]:
    body = build_taxonomy_coverage()
    catalog = tuple(CatalogProduct.model_validate(row) for row in body["catalog"])
    return body, catalog


def test_catalog_padding_stays_multicategory_and_never_claims_real_skus() -> None:
    _body, original = _source()
    assert len(original) == 183
    for size in (1000, 5000):
        products = _catalog_at_size(original, size)
        assert len(products) == len({product.sku for product in products}) == size
        assert products[:len(original)] == original
        assert len({
            product.sku for product in products[len(original):]
            if product.sku.startswith("diagnostic-")
        }) == size - len(original)
        assert {product.category for product in products} == set(CATEGORY_ATTRIBUTES)
        assert all(
            product.attributes == original[index % len(original)].attributes
            for index, product in enumerate(products[len(original):])
        )


def test_positive_query_sample_is_identical_for_all_rankers() -> None:
    body, originals = _source()
    known = {product.sku for product in originals}
    for size, expected in ((1000, 439), (5000, 80)):
        records = _positive_cases(body, count=size)
        assert len(records) == expected
        assert len({record[0] for record in records}) == expected
        assert {record[1].category for record in records} == set(CATEGORY_ATTRIBUTES)
        assert all(record[2] <= known for record in records)
    with pytest.raises(ValueError):
        _positive_cases({"queries": []}, count=1000)


def test_no_match_labels_require_constraints_against_entire_catalog() -> None:
    _body, originals = _source()
    negatives = _negative_cases(originals)
    assert len(negatives) >= 20
    for size in (1000, 5000):
        products = _catalog_at_size(originals, size)
        assert all(
            check_constraints(request, product).status == "incompatible"
            for request in negatives.values()
            for product in products
        )


def test_model_thresholds_are_frozen_and_not_promoted() -> None:
    model, thresholds = _load_frozen_model()
    assert model.dataset_sha256
    assert set(thresholds) == {"ml", "tfidf"}
    assert all(0 <= value <= 1 for value in thresholds.values())


def test_diagnostic_cli_refuses_output_outside_artifacts(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="artifacts"):
        main(["--output", str(tmp_path / "unauthorized.json")])


def test_full_synthetic_diagnostic_blocks_automatic_sku_acceptance() -> None:
    report = build_multicategory_retrieval_report()
    assert report["production_eligible"] is False
    assert report["independent_customer_labels"] is False
    assert report["automatic_matching_release"]["eligible"] is False
    assert report["model_retrained"] is False
    assert report["llm_assisted_evaluated"] is False
    assert len(report["sizes"]) == 2
    assert [row["catalog_skus"] for row in report["sizes"]] == [1000, 5000]
    assert [row["models"]["ml"]["positive_queries"] for row in report["sizes"]] == [
        439, 80,
    ]
    for size in report["sizes"]:
        assert len(size["proven_negative_categories"]) >= 20
        for mode in ("ml", "tfidf", "rapidfuzz"):
            details = size["models"][mode]
            assert set(details["per_category"]) == set(CATEGORY_ATTRIBUTES)
            assert details["false_positive_on_proven_absence"] == 0
            assert 0 <= details["top1_accuracy"] <= details["recall_at_5"] <= 1
        assert size["models"]["ml"]["auto_wrong"] > 0
        assert size["models"]["tfidf"]["auto_wrong"] > 0
