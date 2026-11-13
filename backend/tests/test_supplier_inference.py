"""AI-ready deterministic proposal: no speculative prices, SKU, currency or publication."""
from __future__ import annotations

import io
import json
from pathlib import Path

from openpyxl import load_workbook

from kpdesk.catalog.supplier_inference import inspect_supplier_source

ROOT = Path(__file__).resolve().parents[2] / "ml/data/real/research_candidate_prices"


def test_real_aqualiga_requires_sheet_choice_but_proposes_source_fields() -> None:
    original = ROOT / "aqualiga_sanitary.xlsx"
    book = load_workbook(io.BytesIO(original.read_bytes()), read_only=True)
    try:
        sheet = book.sheetnames[0]
    finally:
        book.close()
    result = inspect_supplier_source(original.name, original.read_bytes())
    assert len(result.available_sheets) == 2
    assert result.selected_sheet is None
    assert "sheet_selection_required" in result.issues
    selected = inspect_supplier_source(original.name, original.read_bytes(), sheet=sheet)
    assert selected.row_count == 3894
    assert selected.selected_sheet == sheet
    assert selected.proposed_fields["name"] == 0
    assert selected.proposed_fields["sku"] == 1
    assert selected.proposed_fields["price"] == 2
    assert selected.proposed_fields["currency"] == 3
    assert selected.proposed_fields["category"] == 4
    assert selected.stage_mapping is not None
    assert selected.stage_mapping["confirmed_currency"] is None
    assert selected.requires_confirmation
    assert not selected.canonical_importable


def test_csv_import_1c_style_columns_can_auto_propose_with_no_llm_cost() -> None:
    raw = ("Код;Наименование;Цена;Валюта;Ед. изм.\n"
           "00021;Бумага офисная;349,00;RUB;пачка\n").encode()
    result = inspect_supplier_source("1c.csv", raw)
    assert result.row_count == 1
    assert result.proposed_fields == {
        "sku": 0, "name": 1, "price": 2, "currency": 3, "sale_unit": 4,
    }
    assert result.stage_mapping is not None
    assert result.stage_mapping["encoding"] == "utf-8-sig"
    assert result.stage_mapping["delimiter"] == ";"
    assert result.stage_mapping["header_row"] == 1
    assert not result.issues


def test_ambiguous_price_tiers_are_not_selected_silently() -> None:
    content = ("Артикул;Наименование;Цена опт;Цена розница\n"
               "01;Кабель;15;20\n").encode()
    result = inspect_supplier_source("supplier.csv", content)
    assert "price" not in result.proposed_fields
    assert "ambiguous_price_columns" in result.issues
    assert "manual_price_selection_required" in result.issues
    assert result.stage_mapping is None
    assert len(result.options["price"]) == 2


def test_json_1c_export_uses_observed_keys_only_and_named_list() -> None:
    raw = json.dumps({"products": [
        {"Код": "0001", "Наименование": "Клавиатура",
         "Цена": "2 500,00", "Валюта": "RUB"},
    ]}, ensure_ascii=False).encode()
    result = inspect_supplier_source("1c.json", raw)
    assert result.items_key == "products"
    assert result.proposed_fields == {
        "sku": "Код", "name": "Наименование", "price": "Цена", "currency": "Валюта",
    }
    assert result.stage_mapping is not None
    assert result.row_count == 1
    assert not result.canonical_importable


def test_unknown_header_does_not_guess_from_prices_or_row_position() -> None:
    source = "Поле 1;Поле 2\nМышь;1450\n".encode()
    result = inspect_supplier_source("strange.csv", source)
    assert "name" not in result.proposed_fields
    assert "price" not in result.proposed_fields
    assert result.stage_mapping is None
    assert "manual_name_selection_required" in result.issues


def test_legacy_binary_xls_header_proposals() -> None:
    source = Path(__file__).parent / "fixtures/supplier_legacy.xls"
    result = inspect_supplier_source(source.name, source.read_bytes())
    assert result.available_sheets == ("Prices",)
    assert result.proposed_fields == {
        "sku": 0, "name": 1, "price": 2, "currency": 3,
    }
    assert result.stage_mapping is not None
    assert result.stage_mapping["sheet"] == "Prices"
    assert result.requires_confirmation


def test_json_duplicate_key_is_rejected_not_trusted() -> None:
    import pytest

    with pytest.raises(ValueError):
        inspect_supplier_source(
            "invalid.json", b'{"products":[{"name":"A","name":"B","price":10}]}'
        )


def test_retail_or_old_price_never_becomes_unqualified_quote_price() -> None:
    result = inspect_supplier_source(
        "old.csv",
        "Название;РРЦ\nТовар А;999\n".encode(),
    )
    assert result.proposed_fields["price"] == 1
    assert "price_basis_confirmation_required" in result.issues
    assert result.requires_confirmation is True
    assert result.canonical_importable is False


def test_cp1251_csv_is_detected_but_requires_encoding_confirmation() -> None:
    result = inspect_supplier_source(
        "old1c.csv",
        "Код;Наименование;Цена\n0001;Бумага;550\n".encode("cp1251"),
    )
    assert result.encoding == "cp1251"
    assert result.row_count == 1
    assert "encoding_confirmation_required" in result.issues
    selected = inspect_supplier_source(
        "old1c.csv",
        "Код;Наименование;Цена\n0001;Бумага;550\n".encode("cp1251"),
        encoding="cp1251",
    )
    assert "encoding_confirmation_required" not in selected.issues


def test_completely_ambiguous_header_row_does_not_yield_auto_mapping() -> None:
    result = inspect_supplier_source(
        "odd.csv",
        "Название;Цена\nНазвание;Цена\nСтрока;100\n".encode(),
    )
    assert "ambiguous_header_row" in result.issues
    assert result.stage_mapping is None


def test_formula_quarantine_is_explicit_for_structural_xlsx_inspection() -> None:
    import io

    import pytest
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "Входящие"
    sheet.append(["Артикул", "Наименование", "Цена", "Валюта", "Пометка"])
    sheet.append(["A-1", "Ручка", 12.345, "RUB", "=2+2"])
    stream = io.BytesIO()
    book.save(stream)
    source = stream.getvalue()
    with pytest.raises(ValueError, match="formula"):
        inspect_supplier_source("supplier.xlsx", source)
    result = inspect_supplier_source(
        "supplier.xlsx", source, formula_policy="quarantine_rows",
    )
    assert result.source_format == "xlsx"
    assert result.proposed_fields == {
        "sku": 0, "name": 1, "price": 2, "currency": 3,
    }
    assert "xlsx_formula_cells_quarantined" in result.issues
    assert result.stage_mapping is not None
    assert result.canonical_importable is False
    assert result.sample_rows[0][2] == "12.345"
    with pytest.raises(ValueError):
        inspect_supplier_source(
            "supplier.xlsx", source, formula_policy="allow",  # type: ignore[arg-type]
        )
