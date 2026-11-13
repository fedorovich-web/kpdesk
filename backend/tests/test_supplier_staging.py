"""Universal, source-faithful supplier staging: no invented SKU, currency or category."""
from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

from kpdesk.catalog.supplier_staging import SupplierMapping, stage_supplier_prices

ROOT = Path(__file__).resolve().parents[2] / "ml/data/real/research_candidate_prices"


def test_real_russian_supplier_aqualiga_mixed_categories_rub_and_missing_units() -> None:
    """Use genuine ORIGINAL Russian supplier file, not a renamed synthetic fixture."""
    original = ROOT / "aqualiga_sanitary.xlsx"
    binary = original.read_bytes()
    book = load_workbook(io.BytesIO(binary), read_only=True)
    try:
        sheet_name = book.sheetnames[0]
    finally:
        book.close()
    staged = stage_supplier_prices(
        original.name, binary,
        SupplierMapping(name=0, sku=1, price=2, currency=3, category=4,
                        header_row=1, sheet=sheet_name),
    )
    assert len(staged.rows) == 3894
    assert staged.rows[0].supplier_sku
    assert staged.rows[0].price_rub is not None
    assert all(item.price_rub is not None for item in staged.rows)
    assert all("sale_unit_requires_review" in item.issues for item in staged.rows)
    assert all("pack_size_requires_review" in item.issues for item in staged.rows)
    assert not staged.canonical_importable
    assert not staged.model_training_authorized


def test_supplier_without_sku_kept_for_resolution_not_discarded() -> None:
    source = "Название;Стоимость\nКлавиатура офисная;2 599,00\nМонитор 27;12 499.50\n"
    stage = stage_supplier_prices(
        "any-shop.csv", source.encode("utf-8"),
        SupplierMapping(name=0, price=1, header_row=1, confirmed_currency="RUB"),
    )
    assert len(stage.rows) == 2
    assert stage.rows[0].price_rub is not None
    assert str(stage.rows[0].price_rub) == "2599.00"
    assert str(stage.rows[1].price_rub) == "12499.50"
    assert stage.rows[0].supplier_sku is None
    assert "missing_supplier_sku" in stage.rows[0].issues
    assert "category_requires_review" in stage.rows[0].issues


def test_no_currency_confirmation_disables_even_numeric_prices() -> None:
    stage = stage_supplier_prices(
        "shop.csv", b"name;price\nProduct;10\n",
        SupplierMapping(name=0, price=1, header_row=1),
    )
    assert stage.rows[0].price_rub is None
    assert "unconfirmed_or_non_rub_currency" in stage.rows[0].issues


def test_currency_column_excludes_eur_and_keeps_rub_without_conversion() -> None:
    stage = stage_supplier_prices(
        "shop.csv", "Товар;Цена;Валюта\nТовар А;10;RUB\nТовар B;10;EUR\n".encode(),
        SupplierMapping(name=0, price=1, currency=2, header_row=1),
    )
    assert str(stage.rows[0].price_rub) == "10"
    assert stage.rows[1].price_rub is None
    assert "unconfirmed_or_non_rub_currency" in stage.rows[1].issues


def test_multi_sheet_xlsx_and_formula_fail_closed() -> None:
    book = Workbook()
    first = book.active
    assert first is not None
    first.title = "Предложение"
    first.append(["Артикул", "Наименование", "Цена"])
    first.append(["0001", "Клавиатура", 1999])
    book.create_sheet("Легенда")
    stream = io.BytesIO()
    book.save(stream)
    no_sheet = SupplierMapping(name=1, price=2, sku=0, header_row=1,
                               confirmed_currency="RUB")
    with pytest.raises(ValueError, match="sheet"):
        stage_supplier_prices("shop.xlsx", stream.getvalue(), no_sheet)
    staged = stage_supplier_prices(
        "shop.xlsx", stream.getvalue(),
        SupplierMapping(name=1, price=2, sku=0, header_row=1,
                        sheet="Предложение", confirmed_currency="RUB"),
    )
    assert staged.rows[0].supplier_sku == "0001"
    assert staged.rows[0].price_rub is not None
    first["C2"] = "=1+1"
    stream = io.BytesIO()
    book.save(stream)
    book.close()
    with pytest.raises(ValueError, match="formula"):
        stage_supplier_prices(
            "shop.xlsx", stream.getvalue(),
            SupplierMapping(name=1, price=2, sku=0, header_row=1,
                            sheet="Предложение", confirmed_currency="RUB"),
        )


def test_ambiguous_duplicate_columns_and_invalid_mapping_refused() -> None:
    with pytest.raises(ValueError, match="mapping"):
        SupplierMapping(name=0, price=0, header_row=1)
    with pytest.raises(ValueError, match="mapping"):
        SupplierMapping(name=-1, price=1, header_row=1)
    with pytest.raises(ValueError, match="mapping"):
        SupplierMapping(name=1, price=2, header_row=0)


def test_cp1251_semicolon_and_numeric_sku_does_not_invent_leading_zeros() -> None:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, delimiter=";", lineterminator="\n")
    writer.writerows([["Название", "Артикул", "Цена"], ["Клавиатура", "00029", "750"]])
    csv_source = buf.getvalue().encode("cp1251")
    staged = stage_supplier_prices(
        "cp1251.csv", csv_source,
        SupplierMapping(name=0, sku=1, price=2, header_row=1, encoding="cp1251",
                        confirmed_currency="RUB"),
    )
    assert staged.rows[0].supplier_sku == "00029"

    book = Workbook()
    page = book.active
    assert page is not None
    page.append(["Название", "Артикул", "Цена"])
    page.append(["Товар", 29, 750])
    stream = io.BytesIO()
    book.save(stream)
    book.close()
    result = stage_supplier_prices("numeric.xlsx", stream.getvalue(),
                                   SupplierMapping(name=0, sku=1, price=2,
                                                   header_row=1, sheet="Sheet",
                                                   confirmed_currency="RUB"))
    assert result.rows[0].supplier_sku is None
    assert "numeric_sku_may_lose_leading_zeros" in result.rows[0].issues


def test_price_parser_rejects_negative_scientific_injected_or_overprecision() -> None:
    text_source = (
        b"name;price\nA;-25.00\nB;1e8\nC;=1+2\nD;0.00\nE;10.123\nF;9,50\n"
    )
    result = stage_supplier_prices(
        "input.csv", text_source,
        SupplierMapping(name=0, price=1, header_row=1, confirmed_currency="RUB"),
    )
    assert [row.price_rub is None for row in result.rows] == [
        True, True, True, True, True, False,
    ]

def test_explicit_ruble_suffix_on_shop_price_needs_no_manual_currency_guess() -> None:
    prices = "Товар;Стоимость\nМонитор;12 499,00 ₽\nБумага;256,43 руб.\nКабель;120 RUB\n".encode()
    result = stage_supplier_prices(
        "public-card.csv", prices,
        SupplierMapping(name=0, price=1, header_row=1),
    )
    assert [str(row.price_rub) for row in result.rows] == [
        "12499.00", "256.43", "120",
    ]
    assert all(
        "unconfirmed_or_non_rub_currency" not in row.issues for row in result.rows
    )
    assert all("missing_supplier_sku" in row.issues for row in result.rows)


def test_ragged_supplier_csv_keeps_reviewable_rows_without_inventing_values() -> None:
    raw = (
        "Артикул;Наименование;Цена;Ед. изм.\n"
        "A-001;Бумага А4;360,00;пачка\n"
        "Раздел: бумага\n"
        "B-002;Папка;95,00\n"
        "C-003;Конверт\n"
        "D-004;Ручка;20,00;шт.\n"
    ).encode()
    mapped = SupplierMapping(
        sku=0, name=1, price=2, sale_unit=3, header_row=1,
        confirmed_currency="RUB",
    )
    staged = stage_supplier_prices("ragged.csv", raw, mapped)
    assert [item.line for item in staged.rows] == [2, 3, 4, 5, 6]
    assert [item.supplier_sku for item in staged.rows] == [
        "A-001", "Раздел: бумага", "B-002", "C-003", "D-004",
    ]
    assert [item.price_rub is not None for item in staged.rows] == [
        True, False, True, False, True,
    ]
    assert "missing_mapped_columns" not in staged.rows[0].issues
    assert "missing_mapped_columns" in staged.rows[1].issues
    assert "missing_name" in staged.rows[1].issues
    assert "invalid_or_missing_price" in staged.rows[1].issues
    assert "missing_mapped_columns" in staged.rows[2].issues
    assert "sale_unit_requires_review" in staged.rows[2].issues
    assert "missing_mapped_columns" in staged.rows[3].issues
    assert "invalid_or_missing_price" in staged.rows[3].issues
    assert staged.rows[3].price_rub is None
    assert "missing_mapped_columns" not in staged.rows[4].issues
    assert not staged.canonical_importable

