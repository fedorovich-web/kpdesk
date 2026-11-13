"""LLM mapping is an untrusted suggestion, not a price rewrite."""
from __future__ import annotations

import pytest

from kpdesk.catalog.supplier_inference import SourceInspection, inspect_supplier_source
from kpdesk.catalog.supplier_model_guard import (
    model_mapping_metadata,
    validate_model_mapping,
)


def _inspection() -> SourceInspection:
    return inspect_supplier_source(
        "shop.csv",
        "Артикул;Наименование;Цена опт;Цена розница\n"
        "00032;Мышь;125;175\n".encode(),
    )


def test_llm_can_suggest_an_existing_ambiguous_price_but_not_publish() -> None:
    found = _inspection()
    data = {
        "source_sha256": found.source_sha256,
        "fields": {"sku": 0, "name": 1, "price": 3},
        "reasoning": "Retail price proposed; user must approve price tier.",
    }
    suggestion = validate_model_mapping(found, data)
    assert suggestion.fields["price"] == 3
    assert suggestion.review_required
    assert suggestion.approved is False
    assert suggestion.publishable is False
    context = model_mapping_metadata(found)
    assert context["columns"] == (
        "Артикул", "Наименование", "Цена опт", "Цена розница",
    )
    assert "125" not in repr(context)


@pytest.mark.parametrize("proposed", [
    {"fields": {"sku": 0, "name": 1, "price": 999}, "reasoning": "invent"},
    {"fields": {"sku": 0, "name": 1, "price": 1}, "reasoning": "duplicate"},
    {"fields": {"sku": 0, "price": 3}, "reasoning": "no name"},
    {"fields": {"name": 1, "price": 3, "stock": 0}, "reasoning": "unknown"},
    {"fields": {"name": 1, "price": 3}, "reasoning": "yes",
     "made_up_price": "10.00"},
])
def test_llm_invented_or_ambiguous_payload_is_rejected(proposed: dict[str, object]) -> None:
    found = _inspection()
    payload = {"source_sha256": found.source_sha256, **proposed}
    with pytest.raises(ValueError):
        validate_model_mapping(found, payload)


def test_llm_cannot_reuse_mapping_for_modified_file() -> None:
    found = _inspection()
    other = inspect_supplier_source(
        "shop.csv", "Артикул;Наименование;Цена\n1;Мышь;200\n".encode()
    )
    with pytest.raises(ValueError, match="different source"):
        validate_model_mapping(other, {
            "source_sha256": found.source_sha256,
            "fields": {"sku": 0, "name": 1, "price": 2},
            "reasoning": "proposed",
        })


def test_json_model_can_only_reference_exact_source_keys() -> None:
    source = b'[{"label":"Mouse","sale":"1299 RUB"}]'
    found = inspect_supplier_source("shop.json", source)
    accepted = validate_model_mapping(found, {
        "source_sha256": found.source_sha256,
        "fields": {"name": "label", "price": "sale"},
        "reasoning": "Observed keys.",
    })
    assert accepted.fields == {"name": "label", "price": "sale"}
    with pytest.raises(ValueError, match="absent JSON"):
        validate_model_mapping(found, {
            "source_sha256": found.source_sha256,
            "fields": {"name": "invented", "price": "sale"},
            "reasoning": "hallucination",
        })
