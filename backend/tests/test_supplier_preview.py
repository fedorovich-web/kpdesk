"""Read-only supplier preview: never treats a column suggestion as approved import."""

import csv
import io
from pathlib import Path

from openpyxl import Workbook

from kpdesk.catalog.schema import ImportLimits
from kpdesk.catalog.supplier_preview import preview_supplier_price
from scripts.build_synthetic_supplier_prices import build_corpus


def test_csv_utf8_bom_columns_proposed_without_approval(tmp_path: Path) -> None:
    generated = tmp_path / "generated"
    manifest = build_corpus(generated)
    entry = manifest["cases"][0]
    preview = preview_supplier_price(entry["file"], (generated / entry["file"]).read_bytes())
    assert preview.row_count == 40
    assert not preview.errors
    assert preview.encoding == "utf-8-sig"
    assert preview.delimiter == ";"
    assert preview.header_row == 1
    assert preview.requires_confirmation
    assert not preview.canonical_importable
    fields = {mapping.field: mapping.source_column for mapping in preview.mappings}
    assert fields["sku"] == "Код"
    assert fields["name"] == "Товар"
    assert fields["price_rub"] == "Цена, руб."
    assert fields["sale_unit"] == "Упаковка"
    assert "category" in preview.missing_fields
    assert len(preview.sample_rows) == 3


def test_cp1251_requires_explicit_encoding_and_supports_tab(tmp_path: Path) -> None:
    generated = tmp_path / "generated"
    manifest = build_corpus(generated)
    cp = manifest["cases"][3]
    rejected = preview_supplier_price(cp["file"], (generated / cp["file"]).read_bytes())
    assert "encoding_confirmation_required" in {error.code for error in rejected.errors}
    accepted = preview_supplier_price(
        cp["file"], (generated / cp["file"]).read_bytes(), encoding_hint="cp1251",
    )
    assert not accepted.errors
    assert accepted.row_count == 40
    tab = manifest["cases"][1]
    tab_preview = preview_supplier_price(tab["file"], (generated / tab["file"]).read_bytes())
    assert tab_preview.delimiter == "\t"
    assert tab_preview.row_count == 40


def test_xlsx_header_offset_and_multiple_sheets_require_selection(tmp_path: Path) -> None:
    generated = tmp_path / "generated"
    manifest = build_corpus(generated)
    multi = manifest["cases"][4]
    content = (generated / multi["file"]).read_bytes()
    ambiguous = preview_supplier_price(multi["file"], content)
    assert "sheet_confirmation_required" in {e.code for e in ambiguous.errors}
    selected = preview_supplier_price(multi["file"], content, sheet_name=multi["sheet"])
    assert selected.row_count == 40
    assert selected.header_row == multi["header_row"]
    assert selected.requires_confirmation
    offset = manifest["cases"][2]
    offset_preview = preview_supplier_price(offset["file"], (generated / offset["file"]).read_bytes())
    assert offset_preview.header_row == 3


def test_duplicate_price_aliases_are_ambiguous_not_accepted() -> None:
    csv_text = "Код;Название;Цена;Стоимость;Ед. изм.\nX;Пример;1.00;2.00;шт.\n"
    proposal = preview_supplier_price("duplicate.csv", csv_text.encode())
    assert "ambiguous_mapping" in {error.code for error in proposal.errors}
    assert not proposal.canonical_importable


def test_xlsx_formula_and_limits_fail_closed() -> None:
    book = Workbook()
    page = book.active
    assert page is not None
    page.title = "Прайс"
    page.append(["Код", "Товар", "Цена", "Ед. изм."])
    page.append(["X", "Набор", "=1+2", "штука"])
    stream = io.BytesIO()
    book.save(stream)
    book.close()
    flagged = preview_supplier_price("formula.xlsx", stream.getvalue())
    assert "formula_forbidden" in {error.code for error in flagged.errors}
    assert flagged.row_count == 0
    rejected = preview_supplier_price(
        "formula.xlsx", stream.getvalue(), limits=ImportLimits(max_upload_bytes=10),
    )
    assert "file_too_large" in {error.code for error in rejected.errors}


def test_empty_and_nonapproved_encoding_fail_without_running_import() -> None:
    assert "file_too_large" in {
        error.code for error in preview_supplier_price("empty.csv", b"").errors
    }
    assert "invalid_encoding" in {
        error.code for error in preview_supplier_price(
            "strange.csv", b"x,y\n1,2", encoding_hint="koi8-r",
        ).errors
    }
    data = io.StringIO(newline="")
    csv.writer(data, delimiter=",").writerows([
        ["Артикул", "Наименование", "Цена", "ЕИ"],
        ["X-1", "Синтетический товар", "4.00", "шт."],
    ])
    preview = preview_supplier_price("demo.csv", data.getvalue().encode())
    assert preview.row_count == 1
    assert preview.requires_confirmation
    assert preview.canonical_importable is False


def test_all_twenty_supplier_price_schemas_are_previewed_without_publication(tmp_path: Path) -> None:
    base = tmp_path / "all"
    manifest = build_corpus(base)
    for entry in manifest["cases"]:
        result = preview_supplier_price(
            entry["file"], (base / entry["file"]).read_bytes(),
            encoding_hint=entry["encoding"] if entry["source_format"] == "csv" else None,
            sheet_name=entry["sheet"],
        )
        assert not result.errors, (entry["case_id"], [e.code for e in result.errors])
        assert result.row_count == 40, entry["case_id"]
        assert result.header_row == entry["header_row"]
        assert result.requires_confirmation
        assert result.canonical_importable is False


def test_supplier_preview_rejects_binary_csv_and_numeric_xlsx_skus() -> None:
    bad_csv = b"sku;name;price\nA\x00;Widget;12.00\n"
    assert "invalid_format" in {
        error.code for error in preview_supplier_price("binary.csv", bad_csv).errors
    }
    book = Workbook()
    page = book.active
    assert page is not None
    page.title = "Products"
    page.append(["Код", "Наименование", "Цена", "Ед. изм."])
    page.append([1234, "Товар с потерянными ведущими нулями", 12.50, "штука"])
    buffer = io.BytesIO()
    book.save(buffer)
    book.close()
    proposal = preview_supplier_price("unsafe.xlsx", buffer.getvalue())
    assert "sku_not_text" in {error.code for error in proposal.errors}
    assert not proposal.canonical_importable

def test_no_sku_supplier_columns_are_previewed_for_review_not_discarded() -> None:
    raw = "Наименование;Цена;Упаковка\\nКлавиатура;2 599,00;шт.\\n".replace("\\n", "\n").encode()
    proposal = preview_supplier_price("dns-office.csv", raw)
    assert not proposal.errors
    assert proposal.row_count == 1
    assert "sku" in proposal.missing_fields
    assert proposal.requires_confirmation
    assert proposal.canonical_importable is False


def test_no_sku_xlsx_office_supply_needs_source_confirmation() -> None:
    book = Workbook()
    ws = book.active
    assert ws is not None
    ws.title = "Supplier"
    ws.append(["Наименование", "Цена"])
    ws.append(["Бумага офисная А4", 250.75])
    stream = io.BytesIO()
    book.save(stream)
    book.close()
    result = preview_supplier_price(
        "officemag.xlsx", stream.getvalue(), sheet_name="Supplier",
    )
    assert not result.errors
    assert "sku" in result.missing_fields
    assert "sale_unit" in result.missing_fields
    assert not result.canonical_importable
