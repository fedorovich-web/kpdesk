"""Confirmed supplier conversion is all-or-nothing and never publishes to a database."""

from __future__ import annotations

import csv
import hashlib
import io
from decimal import Decimal

from openpyxl import Workbook

from kpdesk.catalog.schema import ImportLimits
from kpdesk.catalog.supplier_mapping import ConfirmedSupplierMapping, normalize_supplier_price


def _csv(rows: list[list[str]]) -> bytes:
    stream = io.StringIO(newline="")
    csv.writer(stream, delimiter=";", lineterminator="\n").writerows(rows)
    return stream.getvalue().encode("utf-8-sig")


def _mapping(data: bytes, **changes: object) -> ConfirmedSupplierMapping:
    original: dict[str, object] = {
        "source_sha256": hashlib.sha256(data).hexdigest(),
        "category": "paper",
        "columns": {
            "sku": "Код", "name": "Товар", "price_rub": "Цена, руб.",
            "sale_unit": "Ед.", "paper_format": "Формат", "paper_gsm": "Плотность",
        },
        "unit_values": {"пачка": "пачка"},
        "constants": {"pieces_per_sale_unit": 1},
        "price_semantics": "final_rub",
        "confirmed": True,
    }
    original.update(changes)
    return ConfirmedSupplierMapping(**original)  # type: ignore[arg-type]


def _paper_rows() -> list[list[str]]:
    return [
        ["Код", "Товар", "Цена, руб.", "Ед.", "Формат", "Плотность"],
        ["00001", "Бумага офисная A4 80", "349.90", "пачка", "A4", "80"],
        ["00002", "Бумага офисная A3 120", "950.10", "пачка", "A3", "120"],
    ]


def test_confirmed_paper_price_becomes_two_exact_canonical_products() -> None:
    content = _csv(_paper_rows())
    result = normalize_supplier_price("paper.csv", content, _mapping(content))
    assert result.publishable
    assert result.source_sha256 == hashlib.sha256(content).hexdigest()
    assert [p.sku for p in result.products] == ["00001", "00002"]
    assert result.products[0].price_rub == Decimal("349.90")
    assert result.products[1].paper_gsm == 120
    assert result.products[0].sale_unit == "пачка"


def test_missing_confirmation_or_hash_mismatch_fails_closed() -> None:
    content = _csv(_paper_rows())
    for mapping, code in (
        (_mapping(content, confirmed=False), "mapping_unconfirmed"),
        (_mapping(content, source_sha256="0" * 64), "source_hash_mismatch"),
    ):
        result = normalize_supplier_price("paper.csv", content, mapping)
        assert not result.publishable
        assert not result.products
        assert code in {error.code for error in result.errors}


def test_unknown_category_and_unsupported_unit_do_not_silently_substitute() -> None:
    content = _csv(_paper_rows())
    for mapping, code in (
        (_mapping(content, category="network_switch"), "unsupported_category"),
        (_mapping(content, unit_values={"уп.": "пачка"}), "unmapped_unit"),
        (_mapping(content, constants={"pieces_per_sale_unit": 10}), "invalid_product"),
    ):
        result = normalize_supplier_price("paper.csv", content, mapping)
        assert not result.publishable
        assert not result.products
        assert code in {error.code for error in result.errors}


def test_invalid_price_or_duplicate_sku_rejects_entire_catalog() -> None:
    for column, value, code in (
        (2, "NaN", "invalid_price"),
        (2, "1,255", "invalid_price"),
        (0, "00001", "duplicate_sku"),
    ):
        rows = _paper_rows()
        rows[2][column] = value
        content = _csv(rows)
        result = normalize_supplier_price("paper.csv", content, _mapping(content))
        assert not result.publishable
        assert not result.products
        assert code in {error.code for error in result.errors}


def test_operator_must_acknowledge_vat_included_price_column() -> None:
    rows = _paper_rows()
    rows[0][2] = "Цена с НДС"
    content = _csv(rows)
    mapping = _mapping(content, columns={
        **{k: v for k, v in _mapping(content).columns.items() if k != "price_rub"},
        "price_rub": "Цена с НДС",
    })
    refused = normalize_supplier_price("paper.csv", content, mapping)
    assert not refused.publishable
    assert "price_semantics_mismatch" in {e.code for e in refused.errors}
    approved = normalize_supplier_price(
        "paper.csv", content, _mapping(content, columns=mapping.columns,
                                      price_semantics="vat_included_final_rub"),
    )
    assert approved.publishable
    assert approved.products[0].price_rub == Decimal("349.90")


def test_mapped_xlsx_with_reordered_columns_and_explicit_sheet() -> None:
    book = Workbook()
    intro = book.active
    assert intro is not None
    intro.title = "Описание"
    intro.append(["Synthetic only"])
    sheet = book.create_sheet("Прайс")
    sheet.append(["СИНТЕТИЧЕСКИЙ ТЕСТ"])
    sheet.append(["Цена", "Формат", "Код", "Плотность", "Ед.", "Товар"])
    sheet.append([350.25, "A4", "00001", 80, "пачка", "Бумага офисная"])
    binary = io.BytesIO()
    book.save(binary)
    book.close()
    content = binary.getvalue()
    mapped = _mapping(
        content,
        sheet_name="Прайс",
        columns={
            "price_rub": "Цена", "paper_format": "Формат", "sku": "Код",
            "paper_gsm": "Плотность", "sale_unit": "Ед.", "name": "Товар",
        },
    )
    result = normalize_supplier_price("book.xlsx", content, mapped)
    assert result.publishable, result.errors
    assert result.products[0].price_rub == Decimal("350.25")
    assert result.products[0].sku == "00001"


def test_rejects_duplicate_column_targets_and_unreviewed_extra_fields() -> None:
    content = _csv(_paper_rows())
    for mapping in (
        _mapping(content, columns={"sku": "Код", "name": "Код", "price_rub": "Цена, руб."}),
        _mapping(content, constants={"pieces_per_sale_unit": 1, "unknown": "x"}),
        _mapping(content, columns={**_mapping(content).columns, "brand": "Код"}),
    ):
        result = normalize_supplier_price("paper.csv", content, mapping)
        assert not result.publishable
        assert not result.products
        assert "invalid_mapping" in {e.code for e in result.errors}


def test_no_implicit_required_device_attributes() -> None:
    data = _csv([
        ["Код", "Товар", "Цена, руб.", "Ед."],
        ["SIM-1", "Картридж TK-100 black", "200.00", "штука"],
    ])
    mapping = ConfirmedSupplierMapping(
        source_sha256=hashlib.sha256(data).hexdigest(),
        category="toner_cartridge",
        columns={"sku": "Код", "name": "Товар", "price_rub": "Цена, руб.", "sale_unit": "Ед."},
        unit_values={"штука": "штука"},
        constants={"pieces_per_sale_unit": 1},
        price_semantics="final_rub", confirmed=True,
    )
    result = normalize_supplier_price("toner.csv", data, mapping)
    assert not result.publishable
    assert "invalid_product" in {e.code for e in result.errors}


def test_limits_and_source_integrity_apply_to_conversion() -> None:
    content = _csv(_paper_rows())
    result = normalize_supplier_price(
        "paper.csv", content, _mapping(content),
        limits=ImportLimits(max_upload_bytes=8),
    )
    assert not result.publishable
    assert "file_too_large" in {e.code for e in result.errors}


def test_explicit_typed_attributes_enable_supported_product() -> None:
    content = _csv([
        ["Код", "Товар", "Цена, руб.", "Ед.", "DPI", "Интерфейс"],
        ["M-1", "Мышь 1600 DPI", "450.00", "штука", "1600", "wireless"],
    ])
    mapping = ConfirmedSupplierMapping(
        source_sha256=hashlib.sha256(content).hexdigest(),
        category="mouse",
        columns={"sku": "Код", "name": "Товар", "price_rub": "Цена, руб.", "sale_unit": "Ед."},
        attribute_columns={"dpi": "DPI", "connection": "Интерфейс"},
        unit_values={"штука": "штука"},
        constants={"pieces_per_sale_unit": 1},
        price_semantics="final_rub", confirmed=True,
    )
    result = normalize_supplier_price("mouse.csv", content, mapping)
    assert result.publishable, result.errors
    assert result.products[0].attributes == {"dpi": 1600, "connection": "wireless"}



def test_confirmed_cartridge_compatibility_requires_explicit_reviewed_list() -> None:
    content = _csv([
        ["Код", "Товар", "Цена, руб.", "Ед."],
        ["TON-100", "Картридж тонер TK-100", "2990.00", "штука"],
    ])
    fixed: dict[str, str | int | list[str]] = {
        "cartridge_code": "TK-100",
        "compatible_models": ["Printer-100", "Printer-200"],
        "color": "black",
        "oem_status": "compatible",
    }
    mapping = ConfirmedSupplierMapping(
        source_sha256=hashlib.sha256(content).hexdigest(),
        category="toner_cartridge",
        columns={"sku": "Код", "name": "Товар", "price_rub": "Цена, руб.", "sale_unit": "Ед."},
        unit_values={"штука": "штука"},
        constants={"pieces_per_sale_unit": 1},
        attribute_constants=fixed,
        price_semantics="final_rub", confirmed=True,
    )
    result = normalize_supplier_price("toner.csv", content, mapping)
    assert result.publishable, result.errors
    assert result.products[0].attributes["compatible_models"] == ["Printer-100", "Printer-200"]

    unverified = normalize_supplier_price(
        "toner.csv", content, ConfirmedSupplierMapping(
            source_sha256=mapping.source_sha256, category=mapping.category,
            columns=mapping.columns, unit_values=mapping.unit_values,
            constants=mapping.constants,
            attribute_constants={**fixed, "compatible_models": ["Printer-100", "Printer-100"]},
            price_semantics=mapping.price_semantics, confirmed=True,
        ),
    )
    assert not unverified.publishable
    assert "invalid_product" in {e.code for e in unverified.errors}


def test_confirmation_cannot_be_truthy_nonboolean() -> None:
    content = _csv(_paper_rows())
    mapping = _mapping(content, confirmed="true")
    result = normalize_supplier_price("paper.csv", content, mapping)
    assert not result.publishable
    assert "mapping_unconfirmed" in {e.code for e in result.errors}
