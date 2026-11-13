"""Synthetic 1000/5000-SKU, full-taxonomy supplier staging scale contract.

Checks the actual CSV intake, not catalogue publication, trained-ML
accuracy, or representation of licensed independent supplier data.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from collections import Counter
from decimal import Decimal
from xml.sax.saxutils import escape

import pytest
from openpyxl import Workbook

from kpdesk.catalog.schema import ImportLimits
from kpdesk.catalog.supplier_commerceml import stage_commerceml
from kpdesk.catalog.supplier_json import JsonSupplierMapping, stage_json_supplier_prices
from kpdesk.catalog.supplier_staging import (
    StagedSupplierRow,
    SupplierMapping,
    stage_supplier_prices,
)
from kpdesk.matching.profiles import CATEGORY_ATTRIBUTES
from scripts.build_a46_taxonomy_coverage import build_taxonomy_coverage

_MAPPING = SupplierMapping(
    sku=0, name=1, price=2, currency=3, category=4,
    sale_unit=5, pieces_per_sale_unit=6, header_row=1,
)
_UNITS = ("штука", "упаковка", "коробка", "рулон")
_PACKS = (1, 5, 10, 24)


def _synthetic_supplier_csv(count: int) -> tuple[bytes, tuple[str, ...]]:
    if count < 1:
        raise ValueError("Expected positive synthetic catalog size")
    source = build_taxonomy_coverage()
    catalog = source["catalog"]
    categories = tuple(sorted(CATEGORY_ATTRIBUTES))
    by_category = {
        category: [item["name"] for item in catalog if item["category"] == category]
        for category in categories
    }
    if not all(by_category.values()):
        raise AssertionError("A taxonomy category has no authored examples")
    lines = ["sku;name;price;currency;category;unit;pack"]
    for offset in range(count):
        category = categories[offset % len(categories)]
        names = by_category[category]
        name = names[(offset // len(categories)) % len(names)]
        # A distinct SKU is a generated variant, not a claim that 5000
        # independently sourced real products exist.
        lines.append(
            f"MC-{offset+1:05d};{name} вариант {offset+1:05d};"
            f"{100+offset // 10}.{offset % 100:02d};RUB;{category};"
            f"{_UNITS[offset % len(_UNITS)]};{_PACKS[offset % len(_PACKS)]}"
        )
    return ("\n".join(lines) + "\n").encode("utf-8"), categories


@pytest.mark.parametrize("size", [1000, 5000])
def test_multicategory_supplier_staging_preserves_all_skus_and_source_facts(
    size: int,
) -> None:
    source, categories = _synthetic_supplier_csv(size)
    rows = stage_supplier_prices("multicategory.csv", source, _MAPPING).rows
    assert len(rows) == size
    assert len({row.supplier_sku for row in rows}) == size
    assert {row.source_category for row in rows} == set(categories)
    category_counts = Counter(row.source_category for row in rows)
    assert min(category_counts.values()) >= size // len(categories)
    for offset, row in enumerate(rows):
        assert row.line == offset + 2
        assert row.supplier_sku == f"MC-{offset+1:05d}"
        assert row.name.endswith(f"вариант {offset+1:05d}")
        assert row.price_rub == Decimal(f"{100+offset//10}.{offset%100:02d}")
        assert row.source_sale_unit == _UNITS[offset % len(_UNITS)]
        assert row.source_pack_size == _PACKS[offset % len(_PACKS)]
        assert row.issues == ()
    staged = stage_supplier_prices("multicategory.csv", source, _MAPPING)
    assert staged.canonical_importable is False
    assert staged.model_training_authorized is False
    assert staged.sha256 == hashlib.sha256(source).hexdigest()


def test_multicategory_supplier_csv_rejects_over_5000_rows() -> None:
    source, _ = _synthetic_supplier_csv(ImportLimits().max_rows + 1)
    with pytest.raises(ValueError, match="row|large|limit"):
        stage_supplier_prices("multicategory.csv", source, _MAPPING)


def test_cross_category_supplier_bad_price_remains_unpublishable_evidence() -> None:
    source, _ = _synthetic_supplier_csv(1000)
    source = source.replace(b";RUB;", b";USD;", 1)
    staged = stage_supplier_prices("multicategory.csv", source, _MAPPING)
    assert len(staged.rows) == 1000
    assert staged.rows[0].price_rub is None
    assert "unconfirmed_or_non_rub_currency" in staged.rows[0].issues
    assert staged.canonical_importable is False


def _supplier_rows(size: int) -> list[list[str]]:
    source, _ = _synthetic_supplier_csv(size)
    return list(csv.reader(io.StringIO(source.decode("utf-8")), delimiter=";"))


def _assert_same_staged_facts(
    reference: tuple[StagedSupplierRow, ...],
    candidate: tuple[StagedSupplierRow, ...],
) -> None:
    if len(reference) != len(candidate):
        raise AssertionError("Supplier adapters returned different product counts")
    for original, compared in zip(reference, candidate, strict=True):
        assert (
            compared.line, compared.supplier_sku, compared.name,
            compared.price_rub, compared.source_category,
            compared.source_sale_unit, compared.source_pack_size,
            compared.issues,
        ) == (
            original.line, original.supplier_sku, original.name,
            original.price_rub, original.source_category,
            original.source_sale_unit, original.source_pack_size,
            original.issues,
        )


def _xlsx_source(size: int) -> bytes:
    workbook = Workbook(write_only=True)
    page = workbook.create_sheet("Supplier Prices")
    for row in _supplier_rows(size):
        # Supplier exports store SKUs and prices as text. Do not coerce
        # leading-zero SKUs to Excel numeric cells.
        page.append(row)
    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def _json_source(size: int) -> bytes:
    header, *data = _supplier_rows(size)
    return json.dumps(
        {"products": [dict(zip(header, record, strict=True)) for record in data]},
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")


_JSON_MAPPING = JsonSupplierMapping(
    sku="sku", name="name", price="price", currency="currency",
    category="category", sale_unit="unit", pieces_per_sale_unit="pack",
    items_key="products",
)


@pytest.mark.parametrize("size", [1000, 5000])
def test_csv_xlsx_json_preserve_identical_multicategory_staging(size: int) -> None:
    csv_source, categories = _synthetic_supplier_csv(size)
    original = stage_supplier_prices("supplier.csv", csv_source, _MAPPING)
    xlsx_source = _xlsx_source(size)
    excel = stage_supplier_prices(
        "supplier.xlsx", xlsx_source,
        SupplierMapping(
            name=1, sku=0, price=2, currency=3, category=4,
            sale_unit=5, pieces_per_sale_unit=6, header_row=1,
            sheet="Supplier Prices",
        ),
    )
    json_source = _json_source(size)
    document = stage_json_supplier_prices(
        "supplier.json", json_source, _JSON_MAPPING,
    )
    _assert_same_staged_facts(original.rows, excel.rows)
    _assert_same_staged_facts(original.rows, document.rows)
    assert {row.source_category for row in document.rows} == set(categories)
    assert all(not row.issues for row in original.rows)
    for contents, staged in (
        (csv_source, original), (xlsx_source, excel), (json_source, document),
    ):
        assert staged.sha256 == hashlib.sha256(contents).hexdigest()
        assert staged.canonical_importable is False
        assert staged.model_training_authorized is False


def _commerceml_source(size: int) -> bytes:
    _, *rows = _supplier_rows(size)
    products: list[str] = []
    offers: list[str] = []
    for sku, name, price, currency, _category, unit, _pack in rows:
        identity = "id-" + sku
        products.append(
            "<Товар><Ид>" + identity + "</Ид><Артикул>" + escape(sku)
            + "</Артикул><Наименование>" + escape(name)
            + "</Наименование><БазоваяЕдиница>" + escape(unit)
            + "</БазоваяЕдиница></Товар>"
        )
        offers.append(
            "<Предложение><Ид>" + identity + "</Ид><Цены><Цена>"
            "<ИдТипаЦены>retail</ИдТипаЦены>"
            "<ЦенаЗаЕдиницу>" + escape(price)
            + "</ЦенаЗаЕдиницу><Валюта>" + escape(currency)
            + "</Валюта></Цена></Цены></Предложение>"
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<КоммерческаяИнформация><Каталог><Товары>'
        + "".join(products)
        + "</Товары></Каталог><ПакетПредложений><Предложения>"
        + "".join(offers)
        + "</Предложения></ПакетПредложений></КоммерческаяИнформация>"
    ).encode("utf-8")


def test_commerceml_1000_multicategory_products_require_category_pack_review() -> None:
    original = stage_supplier_prices(
        "supplier.csv", _synthetic_supplier_csv(1000)[0], _MAPPING,
    )
    xml_source = _commerceml_source(1000)
    stage = stage_commerceml("supplier.xml", xml_source, price_type_id="retail")
    assert len(stage.rows) == len(original.rows) == 1000
    assert stage.source_sha256 == hashlib.sha256(xml_source).hexdigest()
    assert stage.selected_price_type_id == "retail"
    assert stage.offered_price_type_ids == ("retail",)
    for from_csv, from_xml in zip(original.rows, stage.rows, strict=True):
        assert from_xml.supplier_sku == from_csv.supplier_sku
        assert from_xml.name == from_csv.name
        assert from_xml.price_rub == from_csv.price_rub
        assert from_xml.source_sale_unit == from_csv.source_sale_unit
        # The current exact-ID CommerceML adapter does not read category
        # or pack. This must remain review-only, not invented equivalence.
        assert from_xml.source_category is None
        assert from_xml.source_pack_size is None
        assert "category_requires_review" in from_xml.issues
        assert "pack_size_requires_review" in from_xml.issues
    assert stage.canonical_importable is False
    assert stage.model_training_authorized is False


def test_commerceml_5000_row_export_fails_closed_at_its_xml_complexity_limit() -> None:
    source = _commerceml_source(5000)
    # The XML node-count bound is tighter than the generic 5000-row bound.
    # A 5000-item supported export requires a separately reviewed design.
    with pytest.raises(ValueError, match="bounded|complexity"):
        stage_commerceml("supplier.xml", source, price_type_id="retail")
