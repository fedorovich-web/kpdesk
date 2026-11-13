"""Deterministic source-schema discovery for the AI-assisted supplier import workflow.

Inspects only bounded, locally uploaded source bytes. No inference of prices,
availability, product category or unit from product text. LLM/ML may later
propose alternative *mappings*, but cannot bypass typed staging validation.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import PurePath
from typing import Any, Literal

import xlrd
from openpyxl import load_workbook

from kpdesk.catalog.parse import _ImportRejected, _validate_xlsx_zip
from kpdesk.catalog.schema import ImportLimits
from kpdesk.catalog.supplier_json import _invalid_constant, _unique_object
from kpdesk.catalog.supplier_staging import (
    SupplierMapping,
    _QuarantinedFormula,
    _source_rows,
)

type SourceColumn = int | str

_FIELDS = ("sku", "name", "price", "currency", "category", "sale_unit",
           "pieces_per_sale_unit")
_ALIASES: dict[str, frozenset[str]] = {
    "sku": frozenset(("sku", "артикул", "код", "код товара", "код поставщика",
                       "штрихкод", "vendorcode", "article", "product code")),
    "name": frozenset(("name", "title", "товар", "наименование", "название",
                        "наименование товара", "название товара", "описание",
                        "номенклатура", "product name", "productname")),
    "price": frozenset(("цена", "стоимость", "price", "retailprice", "price_rub",
                         "цена, руб.", "цена руб", "цена с ндс", "цена (руб.)",
                         "цена (руб)", "цена продажи", "ррц")),
    "currency": frozenset(("валюта", "валюта цены", "currency", "currencycode",
                            'валюта для цены "цена"')),
    "category": frozenset(("category", "категория", "раздел", "название раздела",
                            "группа", "группа товаров", "категория товара")),
    "sale_unit": frozenset(("sale_unit", "ед. изм.", "единица измерения", "ед",
                             "ед.", "еи", "упаковка", "фасовка", "unit")),
    "pieces_per_sale_unit": frozenset((
        "pieces_per_sale_unit", "количество в упаковке",
        "штук в упаковке", "шт. в упаковке", "кол-во в упаковке")),
}
# Recognize labels with clear lexical evidence, not arbitrary semantic guesses.
_PRICE_KEYWORD = re.compile(
    r"(?:^|[\s_])(?:цена|стоимость|price|retailprice)(?:$|[\s_.,(/])"
)
_CURRENCY_KEYWORD = re.compile(r"^(?:валюта|currency)(?:$|[\s_])")
_DELIMITERS = (";", ",", "\t")
_ENCODINGS = ("utf-8-sig", "cp1251")
_MAX_HEADER_ROW = 20


@dataclass(frozen=True, slots=True)
class SourceInspection:
    source_sha256: str
    source_format: str
    row_count: int
    available_sheets: tuple[str, ...] = ()
    selected_sheet: str | None = None
    encoding: str | None = None
    delimiter: str | None = None
    items_key: str | None = None
    header_row: int | None = None
    observed_columns: tuple[str, ...] = ()
    sample_rows: tuple[tuple[str, ...], ...] = ()
    proposed_fields: dict[str, SourceColumn] = dataclass_field(default_factory=dict)
    options: dict[str, tuple[SourceColumn, ...]] = dataclass_field(default_factory=dict)
    stage_mapping: dict[str, Any] | None = None
    issues: tuple[str, ...] = ()
    requires_confirmation: bool = True
    canonical_importable: bool = False
    model_training_authorized: bool = False


def _normalize(text: str) -> str:
    return " ".join(text.casefold().replace("ё", "е").strip().split())


def _field_candidates(name: str) -> tuple[str, ...]:
    label = _normalize(name)
    exact = tuple(field for field in _FIELDS if label in _ALIASES[field])
    if exact:
        return exact
    # "Валюта для цены ..." is a currency, not a price field.
    if _CURRENCY_KEYWORD.search(label):
        return ("currency",)
    if _PRICE_KEYWORD.search(label) or label.startswith("ррц"):
        return ("price",)
    return ()


def _suggest(
    columns: tuple[str, ...],
) -> tuple[dict[str, int], dict[str, tuple[int, ...]], tuple[str, ...]]:
    matched: dict[str, list[int]] = {field: [] for field in _FIELDS}
    for index, header in enumerate(columns):
        for field in _field_candidates(header):
            matched[field].append(index)
    proposed = {field: values[0] for field, values in matched.items()
                if len(values) == 1}
    options = {field: tuple(values) for field, values in matched.items() if values}
    issues: list[str] = []
    for field in ("name", "price"):
        if not matched[field]:
            issues.append(f"manual_{field}_selection_required")
        elif len(matched[field]) > 1:
            issues.append(f"ambiguous_{field}_columns")
            issues.append(f"manual_{field}_selection_required")
    for field in ("sku", "currency", "category", "sale_unit", "pieces_per_sale_unit"):
        if len(matched[field]) > 1:
            issues.append(f"ambiguous_{field}_columns")
    return proposed, options, tuple(issues)


def _tabular_preview(
    filename: str, content: bytes, *,
    sheet: str | None, encoding: str | None, limits: ImportLimits,
    formula_policy: Literal["reject", "quarantine_rows"],
) -> tuple[list[tuple[int, list[object]]], tuple[str, ...],
           str | None, str | None, str | None, tuple[str, ...]]:
    suffix = PurePath(filename).suffix.casefold()
    sheet_names: tuple[str, ...] = ()
    selected_sheet: str | None = None
    detected_encoding: str | None = None
    delimiter: str | None = None
    if suffix == ".csv":
        if b"\x00" in content or content.startswith(b"PK\x03\x04"):
            raise ValueError("Invalid CSV source.")
        candidates: list[tuple[int, str, str, list[tuple[int, list[object]]]]] = []
        encodings = (encoding,) if encoding else _ENCODINGS
        for codec in encodings:
            if codec not in _ENCODINGS:
                raise ValueError("Invalid source encoding.")
            try:
                decoded = content.decode(codec, errors="strict")
            except UnicodeError:
                continue
            for separator in _DELIMITERS:
                try:
                    rows = list(csv.reader(
                        io.StringIO(decoded, newline=""), delimiter=separator, strict=True,
                    ))
                except csv.Error:
                    continue
                if not rows or len(rows) > limits.max_rows + _MAX_HEADER_ROW:
                    continue
                if any(len(row) > 64 or any(len(x) > limits.max_cell_chars for x in row)
                       for row in rows):
                    continue
                sample = rows[:_MAX_HEADER_ROW]
                signal = max((
                    len(_suggest(tuple(row))[0]) for row in sample
                ), default=0)
                width = max((len(row) for row in sample), default=0)
                if width >= 2:
                    candidates.append((
                        signal * 100 + min(width, 64), codec, separator,
                        [(index, list(row)) for index, row in enumerate(rows, 1)],
                    ))
            # UTF-8 is authoritative when valid: no CP1251 fallback reinterpretation.
            if candidates:
                break
        if not candidates:
            raise ValueError("Cannot safely detect CSV delimiter and encoding.")
        candidates.sort(key=lambda x: x[0], reverse=True)
        if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
            raise ValueError("Ambiguous CSV structure: select delimiter manually.")
        _, detected_encoding, delimiter, result = candidates[0]
        return result, sheet_names, selected_sheet, detected_encoding, delimiter, ()

    if suffix == ".xlsx":
        try:
            _validate_xlsx_zip(content, limits)
            book = load_workbook(
                io.BytesIO(content), read_only=True, data_only=False, keep_links=False,
            )
            try:
                sheet_names = tuple(book.sheetnames)
            finally:
                book.close()
        except _ImportRejected as error:
            raise ValueError("Unsafe XLSX workbook.") from error
    elif suffix == ".xls":
        if not content.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
            raise ValueError("Invalid XLS signature.")
        try:
            book_xls = xlrd.open_workbook(file_contents=content, on_demand=True)
            try:
                sheet_names = tuple(book_xls.sheet_names())
            finally:
                book_xls.release_resources()
        except xlrd.XLRDError as error:  # type: ignore[attr-defined]
            raise ValueError("Invalid XLS workbook.") from error
    else:
        raise ValueError("Only CSV, XLS, XLSX and JSON are inspected.")

    if sheet is not None and sheet not in sheet_names:
        raise ValueError("Sheet is absent from supplier workbook.")
    if sheet is None and len(sheet_names) > 1:
        return [], sheet_names, None, None, None, ("sheet_selection_required",)
    if not sheet_names:
        raise ValueError("Supplier workbook has no sheets.")
    selected_sheet = sheet or sheet_names[0]
    # Use the already tested format-specific bounded reader with an inert dummy
    # mapping. No SKU, currency or product category inference occurs here.
    dummy = SupplierMapping(name=0, price=1, header_row=1, sheet=selected_sheet)
    result = _source_rows(
        filename, content, dummy, limits, formula_policy=formula_policy,
    )
    return result, sheet_names, selected_sheet, None, None, ()


def _json_preview(
    content: bytes, limits: ImportLimits,
) -> tuple[list[tuple[int, list[object]]], str | None]:
    try:
        source: object = json.loads(
            content.decode("utf-8-sig", errors="strict"),
            object_pairs_hook=_unique_object, parse_constant=_invalid_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("Invalid supplier JSON.") from error
    items_key: str | None = None
    if isinstance(source, list):
        items = source
    elif isinstance(source, dict):
        if len(source) != 1:
            raise ValueError("Ambiguous JSON collection, choose supported product list.")
        items_key = next(iter(source))
        if items_key not in ("products", "items", "offers", "data"):
            raise ValueError("Unknown JSON product collection.")
        items = source[items_key]
    else:
        raise TypeError("JSON source must contain a product array.")
    if not isinstance(items, list) or not 1 <= len(items) <= limits.max_rows:
        raise ValueError("JSON source array outside row limits.")
    fields: list[str] = []
    for item in items[:20]:
        if not isinstance(item, dict) or len(item) > 64:
            raise ValueError("JSON rows must contain flat product objects.")
        for field in item:
            if not isinstance(field, str) or len(field) > 100:
                raise ValueError("Invalid JSON source key.")
            if field not in fields:
                fields.append(field)
    if len(fields) < 2 or len(fields) > 64:
        raise ValueError("JSON source needs at least two bounded properties.")
    rows: list[tuple[int, list[object]]] = [(1, list(fields))]
    for line, item in enumerate(items, 2):
        if not isinstance(item, dict) or len(item) > 64:
            raise ValueError("Invalid JSON product row.")
        values: list[object] = []
        for field in fields:
            value = item.get(field)
            if type(value) not in (str, int, float, type(None)):
                raise ValueError("Unsupported nested or Boolean JSON source value.")
            if value is not None and len(str(value)) > limits.max_cell_chars:
                raise ValueError("JSON value exceeds safe cell size.")
            values.append(value)
        rows.append((line, values))
    return rows, items_key


def inspect_supplier_source(
    filename: str, content: bytes, *,
    sheet: str | None = None, encoding: str | None = None,
    limits: ImportLimits | None = None,
    formula_policy: Literal["reject", "quarantine_rows"] = "reject",
) -> SourceInspection:
    """Return evidence-only schema hints, *never* an auto-accepted quote catalog."""
    if formula_policy not in ("reject", "quarantine_rows"):
        raise ValueError("Unsupported supplier formula policy.")
    limits = limits or ImportLimits()
    if not filename or not content or len(content) > limits.max_upload_bytes:
        raise ValueError("Invalid source name, empty content or upload too large.")
    suffix = PurePath(filename).suffix.casefold()
    if suffix not in (".csv", ".xls", ".xlsx", ".json"):
        raise ValueError("Unsupported supplier inspection source format.")
    digest = hashlib.sha256(content).hexdigest()
    items_key: str | None = None
    if suffix == ".json":
        rows, items_key = _json_preview(content, limits)
        sheets: tuple[str, ...] = ()
        selected_sheet = None
        detected_encoding = None
        delimiter = None
        early_issues: tuple[str, ...] = ()
    else:
        rows, sheets, selected_sheet, detected_encoding, delimiter, early_issues = (
            _tabular_preview(
                filename, content, sheet=sheet, encoding=encoding,
                limits=limits, formula_policy=formula_policy,
            )
        )
    if early_issues:
        return SourceInspection(
            source_sha256=digest, source_format=suffix.lstrip("."),
            row_count=0, available_sheets=sheets, issues=early_issues,
        )
    if not rows:
        raise ValueError("No data in supplier source.")
    if suffix == ".json":
        best_index = 0
        columns = tuple(str(value) for value in rows[0][1])
    else:
        header_candidates: list[tuple[int, int, tuple[str, ...]]] = []
        for offset, (_, row) in enumerate(rows[:_MAX_HEADER_ROW]):
            if not row or len(row) > 64:
                continue
            names = tuple(str(value).strip() if value is not None else "" for value in row)
            proposed, _, _ = _suggest(names)
            header_candidates.append((len(proposed), offset, names))
        if not header_candidates:
            raise ValueError("No usable supplier headers.")
        maximum = max(entry[0] for entry in header_candidates)
        winners = [entry for entry in header_candidates if entry[0] == maximum]
        if len(winners) > 1:
            header_ambiguity = True
        else:
            header_ambiguity = False
        best_index = winners[0][1]
        columns = winners[0][2]
    proposed, options, issues = _suggest(columns)
    warnings = list(issues)
    if any(isinstance(value, _QuarantinedFormula) for _, row in rows for value in row):
        warnings.append("xlsx_formula_cells_quarantined")
    if suffix != ".json" and header_ambiguity:
        warnings.append("ambiguous_header_row")
    if (
        suffix != ".json" and len(rows) > 1
        and len({col for col in columns if col}) < len([col for col in columns if col])
    ):
        warnings.append("duplicate_source_headers")
    if suffix == ".csv" and detected_encoding == "cp1251" and encoding is None:
        warnings.append("encoding_confirmation_required")
    if suffix == ".xls":
        warnings.append("xls_formula_and_macro_provenance_unverified")
    if "price" in proposed:
        price_header = _normalize(columns[proposed["price"]])
        if any(fragment in price_header for fragment in (
            "опт", "розниц", "ррц", "старая", "со скид", "без ндс", "с ндс",
            "цена продажи", "по карте",
        )):
            warnings.append("price_basis_confirmation_required")
    # A selected single header is required to create a safe suggested mapping.
    mapping: dict[str, Any] | None = None
    if "name" in proposed and "price" in proposed and not any(
        issue.startswith("ambiguous_") for issue in warnings
    ):
        mapping = {"name": proposed["name"], "price": proposed["price"],
                   "sku": proposed.get("sku"), "currency": proposed.get("currency"),
                   "category": proposed.get("category"),
                   "sale_unit": proposed.get("sale_unit"),
                   "pieces_per_sale_unit": proposed.get("pieces_per_sale_unit"),
                   "confirmed_currency": None}
        if suffix == ".json":
            mapping["items_key"] = items_key
        else:
            mapping.update({
                "header_row": best_index + 1, "sheet": selected_sheet,
                "encoding": detected_encoding or "utf-8-sig",
                "delimiter": delimiter or ";",
            })
    return SourceInspection(
        source_sha256=digest, source_format=suffix.lstrip("."),
        row_count=len(rows) - best_index - 1,
        available_sheets=sheets, selected_sheet=selected_sheet,
        encoding=detected_encoding, delimiter=delimiter, items_key=items_key,
        header_row=best_index + 1,
        observed_columns=columns,
        sample_rows=tuple(
            tuple(
                "" if isinstance(value, _QuarantinedFormula) or value is None
                else str(value)[:180] for value in values[:12]
            )
            for _, values in rows[best_index + 1:best_index + 4]
        ),
        proposed_fields={
            field: columns[index] if suffix == ".json" else index
            for field, index in proposed.items()
        },
        options={
            field: tuple(columns[index] if suffix == ".json" else index for index in candidates)
            for field, candidates in options.items()
        },
        stage_mapping=mapping if suffix != ".json" else (
            {key: columns[value] if isinstance(value, int) else value
             for key, value in mapping.items()} if mapping else None
        ),
        issues=tuple(warnings),
    )
