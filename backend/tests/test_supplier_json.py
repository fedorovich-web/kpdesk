"""Real JSON supplier structures normalized with no made-up category or price."""
from __future__ import annotations

import json

import pytest

from kpdesk.catalog.supplier_json import JsonSupplierMapping, stage_json_supplier_prices


def test_1c_like_json_products_with_different_field_names() -> None:
    original = json.dumps({"products": [
        {"Код": "000031", "Наименование": "Бумага А4", "Цена": "459,90",
         "Валюта": "RUB", "Ед": "пачка"},
        {"Код": "000032", "Наименование": "Мышь", "Цена": "825.00",
         "Валюта": "RUB", "Ед": "штука"},
    ]}, ensure_ascii=False).encode()
    result = stage_json_supplier_prices(
        "1c-export.json", original,
        JsonSupplierMapping(name="Наименование", price="Цена", sku="Код",
                            currency="Валюта", sale_unit="Ед", items_key="products"),
    )
    assert len(result.rows) == 2
    assert result.rows[0].supplier_sku == "000031"
    assert str(result.rows[0].price_rub) == "459.90"
    assert result.rows[0].source_sale_unit == "пачка"
    assert "category_requires_review" in result.rows[0].issues
    assert result.canonical_importable is False
    assert result.model_training_authorized is False


def test_root_array_and_explicit_rub_suffix_without_sku() -> None:
    original = b'[{"title":"Cable","sale":"1 299.50 RUB"}]'
    result = stage_json_supplier_prices(
        "external.json", original, JsonSupplierMapping(name="title", price="sale"),
    )
    assert str(result.rows[0].price_rub) == "1299.50"
    assert result.rows[0].supplier_sku is None


def test_numeric_json_sku_cannot_be_silently_recast() -> None:
    original = b'[{"name":"Mouse","price":599,"sku":12}]'
    result = stage_json_supplier_prices(
        "shop.json", original,
        JsonSupplierMapping(name="name", price="price", sku="sku",
                            confirmed_currency="RUB"),
    )
    assert result.rows[0].supplier_sku is None
    assert "numeric_sku_may_lose_leading_zeros" in result.rows[0].issues


@pytest.mark.parametrize("source", [
    b'{"products":[{"name":"A","price":10,"price":999}]}',
    b'[{"name":"A","price":NaN}]',
    b'{"items":[{"name":"A","price":10}],"secret":"x"}',
    b'{"items":[{"name":{"nested":true},"price":10}]}',
    b'{"items":[]}',
])
def test_bad_or_ambiguous_source_rejected(source: bytes) -> None:
    with pytest.raises(ValueError):
        stage_json_supplier_prices(
            "shop.json", source, JsonSupplierMapping(
                name="name", price="price", items_key="items",
                confirmed_currency="RUB",
            ),
        )


def test_json_duplicate_mapping_or_nested_path_refused() -> None:
    with pytest.raises(ValueError):
        JsonSupplierMapping(name="title", price="title")
    with pytest.raises(ValueError):
        JsonSupplierMapping(name="goods.0", price="price")


def test_unverified_currency_does_not_become_rub_price() -> None:
    result = stage_json_supplier_prices(
        "shop.json", b'[{"name":"Product","price":550}]',
        JsonSupplierMapping(name="name", price="price"),
    )
    assert result.rows[0].price_rub is None
    assert "unconfirmed_or_non_rub_currency" in result.rows[0].issues
