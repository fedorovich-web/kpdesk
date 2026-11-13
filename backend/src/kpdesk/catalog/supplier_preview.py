"""Bounded, read-only mapping proposals for supplier CSV/XLSX.

This module never imports, persists or publishes products. An operator must
confirm source meaning (tax, package conversion, category, compatibility).
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from pathlib import PurePath
from zipfile import BadZipFile

from openpyxl import load_workbook

from kpdesk.catalog.parse import _ImportRejected, _validate_xlsx_zip
from kpdesk.catalog.schema import ImportErrorDetail, ImportLimits

_MAX_HEADER_ROW = 6
_MAX_COLUMNS = 64
_DELIMITERS = (";", ",", "\t")
_ENCODINGS = frozenset(("utf-8", "utf-8-sig", "cp1251"))

# Only unambiguous literal aliases. No semantic inference of tax or units.
_ALIASES: dict[str, frozenset[str]] = {
    "sku": frozenset(("код", "артикул", "код поставщика", "код товара", "sku")),
    "name": frozenset(("товар", "наименование", "позиция", "описание", "номенклатура", "название")),
    "price_rub": frozenset((
        "цена", "стоимость", "цена, руб.", "цена с ндс", "цена rub", "цена руб",
    )),
    "sale_unit": frozenset(("упаковка", "еи", "ед. изм.", "фасовка")),
    "pieces_per_sale_unit": frozenset(("штук в упаковке",)),
    "supplier_variant": frozenset((
        "исполнение", "вариант", "размер", "характеристика", "техническое исполнение",
    )),
}


@dataclass(frozen=True, slots=True)
class ColumnSuggestion:
    field: str
    source_column: str
    index: int


@dataclass(frozen=True, slots=True)
class SupplierPreview:
    source_sha256: str
    encoding: str | None
    delimiter: str | None
    sheet: str | None
    header_row: int | None
    row_count: int
    mappings: tuple[ColumnSuggestion, ...]
    missing_fields: tuple[str, ...]
    sample_rows: tuple[tuple[str, ...], ...]
    errors: tuple[ImportErrorDetail, ...]
    requires_confirmation: bool = True
    canonical_importable: bool = False


def _normalized(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").strip().split())


def _source_field(value: str) -> str | None:
    cleaned = _normalized(value)
    return next((field for field, aliases in _ALIASES.items() if cleaned in aliases), None)


def _header_choice(rows: list[list[str]]) -> tuple[int, list[ColumnSuggestion], bool] | None:
    candidates: list[tuple[int, int, list[ColumnSuggestion], bool]] = []
    for line_index, cells in enumerate(rows[:_MAX_HEADER_ROW]):
        suggestions = [
            ColumnSuggestion(field, cell, index)
            for index, cell in enumerate(cells)
            if (field := _source_field(cell)) is not None
        ]
        fields = [suggestion.field for suggestion in suggestions]
        # A genuine supplier may omit SKU; keep its rows for explicit review.
        if {"name", "price_rub"}.issubset(fields):
            candidates.append((
                len(set(fields)), line_index, suggestions,
                len(set(fields)) != len(fields),
            ))
    if not candidates:
        return None
    largest = max(entry[0] for entry in candidates)
    best = [entry for entry in candidates if entry[0] == largest]
    if len(best) != 1:
        return None
    _, index, suggestions, duplicates = best[0]
    return index, suggestions, duplicates


def _csv_rows(
    content: bytes, encoding_hint: str | None, limits: ImportLimits,
) -> tuple[list[list[str]], str, str]:
    if b"\x00" in content:
        raise _ImportRejected("invalid_format", "CSV содержит нулевые байты.")
    if encoding_hint is not None and encoding_hint not in _ENCODINGS:
        raise _ImportRejected("invalid_encoding", "Неизвестная кодировка.")
    encoding = encoding_hint or "utf-8-sig"
    try:
        decoded = content.decode(encoding, errors="strict")
    except UnicodeDecodeError as error:
        if encoding_hint is None:
            raise _ImportRejected(
                "encoding_confirmation_required", "Подтвердите кодировку CSV.",
            ) from error
        raise _ImportRejected("invalid_encoding", "Некорректная кодировка CSV.") from error
    scored: list[tuple[int, str, list[list[str]]]] = []
    for delimiter in _DELIMITERS:
        try:
            values = list(csv.reader(io.StringIO(decoded, newline=""),
                                     delimiter=delimiter, strict=True))
        except csv.Error:
            continue
        if len(values) > limits.max_rows + _MAX_HEADER_ROW:
            raise _ImportRejected("too_many_rows", "Превышено число строк.")
        if any(len(row) > _MAX_COLUMNS for row in values):
            continue
        proposal = _header_choice(values)
        if proposal is not None:
            scored.append((len(proposal[1]), delimiter, values))
    if not scored:
        raise _ImportRejected("invalid_header", "Неизвестные заголовки CSV.")
    highest = max(item[0] for item in scored)
    options = [item for item in scored if item[0] == highest]
    if len(options) != 1:
        raise _ImportRejected("ambiguous_delimiter", "Неоднозначный разделитель.")
    _, separator, rows = options[0]
    return rows, encoding, separator


def _xlsx_rows(
    content: bytes, sheet_name: str | None, limits: ImportLimits,
) -> tuple[list[list[str]], str, frozenset[tuple[int, int]]]:
    _validate_xlsx_zip(content, limits)
    try:
        workbook = load_workbook(
            io.BytesIO(content), read_only=True, data_only=False, keep_links=False,
        )
        try:
            if sheet_name is None and len(workbook.sheetnames) != 1:
                raise _ImportRejected(
                    "sheet_confirmation_required", "Выберите лист с товарами.",
                )
            if sheet_name is not None and sheet_name not in workbook.sheetnames:
                raise _ImportRejected("invalid_sheet", "Выбранный лист отсутствует.")
            name = sheet_name or workbook.sheetnames[0]
            sheet = workbook[name]
            if sheet.max_row is not None and sheet.max_row > limits.max_rows + _MAX_HEADER_ROW:
                raise _ImportRejected("too_many_rows", "Превышено число строк XLSX.")
            if sheet.max_column is not None and sheet.max_column > _MAX_COLUMNS:
                raise _ImportRejected("too_many_columns", "Слишком много столбцов.")
            width = sheet.max_column or _MAX_COLUMNS
            rows: list[list[str]] = []
            non_text_cells: set[tuple[int, int]] = set()
            for number, cells in enumerate(
                sheet.iter_rows(max_row=limits.max_rows + _MAX_HEADER_ROW, max_col=width),
                start=1,
            ):
                values: list[str] = []
                for column_index, cell in enumerate(cells):
                    if cell.data_type == "f":
                        raise _ImportRejected(
                            "formula_forbidden", "Формулы XLSX запрещены.", number,
                        )
                    raw = cell.value
                    if isinstance(raw, bool):
                        raise _ImportRejected(
                            "invalid_cell_type", "Логический тип ячейки запрещён.", number,
                        )
                    if raw is not None and not isinstance(raw, str):
                        non_text_cells.add((number, column_index))
                    values.append("" if raw is None else str(raw))
                while values and not values[-1]:
                    values.pop()
                rows.append(values)
            return rows, name, frozenset(non_text_cells)
        finally:
            workbook.close()
    except _ImportRejected:
        raise
    except (BadZipFile, OSError, ValueError, TypeError, KeyError) as error:
        raise _ImportRejected("invalid_xlsx", "Повреждённая таблица XLSX.") from error


def _failure(
    content: bytes, error: ImportErrorDetail, *, encoding: str | None = None,
    delimiter: str | None = None, sheet: str | None = None,
) -> SupplierPreview:
    return SupplierPreview(
        hashlib.sha256(content).hexdigest(), encoding, delimiter, sheet, None,
        0, (), (), (), (error,),
    )


def preview_supplier_price(
    filename: str, content: bytes, *,
    encoding_hint: str | None = None,
    sheet_name: str | None = None,
    limits: ImportLimits | None = None,
) -> SupplierPreview:
    """Propose mappings, rejecting unsafe inputs; never validates a purchasable SKU."""
    limits = limits or ImportLimits()
    if not content or len(content) > limits.max_upload_bytes:
        return _failure(content, ImportErrorDetail(
            "file_too_large", "Пустой файл или превышен лимит.",
        ))
    suffix = PurePath(filename).suffix.lower()
    if suffix not in (".csv", ".xlsx"):
        return _failure(content, ImportErrorDetail(
            "invalid_extension", "Требуется CSV или XLSX.",
        ))
    if content.startswith(b"PK\x03\x04") and suffix == ".csv":
        return _failure(content, ImportErrorDetail("invalid_format", "Не текстовый CSV."))
    encoding: str | None = None
    delimiter: str | None = None
    sheet: str | None = None
    non_text_cells: frozenset[tuple[int, int]] = frozenset()
    try:
        if suffix == ".csv":
            rows, encoding, delimiter = _csv_rows(content, encoding_hint, limits)
        else:
            rows, sheet, non_text_cells = _xlsx_rows(content, sheet_name, limits)
        for line_number, row in enumerate(rows, start=1):
            if any(len(value) > limits.max_cell_chars for value in row):
                raise _ImportRejected(
                    "cell_too_long", "Слишком длинная ячейка.", line_number,
                )
        choice = _header_choice(rows)
        if choice is None:
            raise _ImportRejected("invalid_header", "Шапка не определена.")
        index, mappings, duplicated = choice
        if duplicated:
            raise _ImportRejected(
                "ambiguous_mapping", "Несколько колонок для одного поля.", index + 1,
            )
        sku_column = next(
            (mapping.index for mapping in mappings if mapping.field == "sku"), None
        )
        for row_number in range(index + 2, len(rows) + 1):
            if sku_column is not None and (row_number, sku_column) in non_text_cells:
                raise _ImportRejected(
                    "sku_not_text", "Артикул в XLSX должен быть текстом.", row_number,
                )
        nonempty = [row for row in rows[index + 1:] if any(row)]
        if len(nonempty) > limits.max_rows:
            raise _ImportRejected("too_many_rows", "Превышено количество товаров.")
        if not nonempty:
            raise _ImportRejected("empty_catalog", "Нет товарных строк.")
        width = len(rows[index])
        for offset, row in enumerate(rows[index + 1:], start=index + 2):
            if not any(row) or len(row) != width:
                raise _ImportRejected("invalid_row_width", "Неверная строка.", offset)
        present = {mapping.field for mapping in mappings}
        missing = tuple(sorted(
            {"sku", "name", "price_rub", "sale_unit", "category"} - present
        ))
        return SupplierPreview(
            hashlib.sha256(content).hexdigest(), encoding, delimiter, sheet, index + 1,
            len(nonempty), tuple(mappings), missing,
            tuple(tuple(row) for row in nonempty[:3]), (),
        )
    except _ImportRejected as error:
        return _failure(
            content, error.error, encoding=encoding, delimiter=delimiter, sheet=sheet,
        )
