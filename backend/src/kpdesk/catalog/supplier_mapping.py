"""Pure, all-or-nothing supplier-to-canonical conversion after explicit operator mapping.

No tenant identity, permission grant, DB write, inference or catalog publishing here.
A caller must authenticate the operator before using this output for any later workflow.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from kpdesk.catalog.parse import _ImportRejected, _parse_positive_integer, _parse_row
from kpdesk.catalog.schema import (
    COLUMNS_V2,
    CatalogImportResult,
    ImportErrorDetail,
    ImportLimits,
)
from kpdesk.catalog.supplier_preview import _csv_rows, _xlsx_rows, preview_supplier_price
from kpdesk.matching.profiles import CATEGORY_ATTRIBUTES

type LiteralField = str | int | list[str]
type PriceSemantics = Literal["final_rub", "vat_included_final_rub"]

_CORE = frozenset(("sku", "name", "price_rub"))
_SUPPORTED_COLUMNS = frozenset(COLUMNS_V2) - {"category", "attributes_json"}
_ATTRS_UNSUPPORTED = frozenset(("attributes", "category", "attributes_json"))


@dataclass(frozen=True, slots=True)
class ConfirmedSupplierMapping:
    source_sha256: str
    category: str
    columns: Mapping[str, str]
    unit_values: Mapping[str, str]
    constants: Mapping[str, str | int]
    price_semantics: PriceSemantics
    confirmed: bool
    attribute_columns: Mapping[str, str] = field(default_factory=dict)
    attribute_constants: Mapping[str, LiteralField] = field(default_factory=dict)
    encoding_hint: str | None = None
    sheet_name: str | None = None


def _error(code: str, message: str, row: int | None = None, field: str | None = None
           ) -> ImportErrorDetail:
    return ImportErrorDetail(code, message, row, field)


def _safe_result(filename: str, digest: str, error: ImportErrorDetail) -> CatalogImportResult:
    return CatalogImportResult(filename, digest, (), (error,))


def _approved_mapping_error(
    mapping: ConfirmedSupplierMapping, headers: list[str],
) -> ImportErrorDetail | None:
    fields = set(mapping.columns)
    static_fields = set(mapping.constants)
    extras = set(mapping.attribute_columns)
    fixed_extras = set(mapping.attribute_constants)
    if (
        not _CORE.issubset(fields | static_fields)
        or not fields <= _SUPPORTED_COLUMNS
        or not static_fields <= _SUPPORTED_COLUMNS
        or fields & static_fields
        or "sku" in static_fields or "name" in static_fields or "price_rub" in static_fields
        or not {"sku", "name", "price_rub"}.issubset(fields)
        or "sale_unit" not in (fields | static_fields)
        or "pieces_per_sale_unit" not in (fields | static_fields)
        or not all(isinstance(key, str) and isinstance(value, str)
                   for key, value in mapping.columns.items())
        or not all(isinstance(key, str) and isinstance(value, str)
                   for key, value in mapping.attribute_columns.items())
    ):
        return _error("invalid_mapping", "Недопустимое сопоставление полей.")
    names = list(mapping.columns.values()) + list(mapping.attribute_columns.values())
    if (
        len(names) != len(set(names))
        or len(headers) != len(set(headers))
        or any(not name.strip() or name not in headers for name in names)
        or len(extras) != len(set(extras))
        or extras & fixed_extras
        or extras & _ATTRS_UNSUPPORTED
        or fixed_extras & _ATTRS_UNSUPPORTED
    ):
        return _error("invalid_mapping", "Неоднозначные исходные поля.")
    allowed_attributes = {item.name for item in CATEGORY_ATTRIBUTES[mapping.category]}
    if not (extras | fixed_extras) <= allowed_attributes:
        return _error("invalid_mapping", "Неизвестные атрибуты категории.")
    if any(type(value) not in (str, int) for value in mapping.constants.values()):
        return _error("invalid_mapping", "Недопустимая константа поля.")
    if any(type(value) not in (str, int, list) for value in mapping.attribute_constants.values()):
        return _error("invalid_mapping", "Недопустимая константа атрибута.")
    if any(
        not isinstance(raw, str) or not isinstance(value, str)
        or value not in ("штука", "пачка", "коробка")
        for raw, value in mapping.unit_values.items()
    ):
        return _error("invalid_mapping", "Неподдерживаемое сопоставление единиц.")
    source_price_column = mapping.columns["price_rub"].casefold()
    if "с ндс" in source_price_column and mapping.price_semantics != "vat_included_final_rub":
        return _error("price_semantics_mismatch", "Требуется подтвердить цену с НДС.")
    if mapping.price_semantics not in ("final_rub", "vat_included_final_rub"):
        return _error("invalid_mapping", "Неизвестная трактовка цены.")
    return None


def _rows_for_conversion(
    filename: str, content: bytes, mapping: ConfirmedSupplierMapping, limits: ImportLimits,
) -> tuple[list[list[str]], frozenset[tuple[int, int]]]:
    if filename.casefold().endswith(".csv"):
        rows, _, _ = _csv_rows(content, mapping.encoding_hint, limits)
        return rows, frozenset()
    rows, _, non_text = _xlsx_rows(content, mapping.sheet_name, limits)
    return rows, non_text


def _attributes(
    row: list[str], header_indices: Mapping[str, int], mapping: ConfirmedSupplierMapping,
) -> dict[str, LiteralField]:
    result: dict[str, LiteralField] = dict(mapping.attribute_constants)
    rules = {rule.name: rule for rule in CATEGORY_ATTRIBUTES[mapping.category]}
    for attribute, column in mapping.attribute_columns.items():
        raw = row[header_indices[column]].strip()
        if not raw:
            continue  # Required values are rejected by CatalogProduct validation.
        if rules[attribute].value_kind is int:
            result[attribute] = _parse_positive_integer(raw)
        elif rules[attribute].value_kind is str:
            result[attribute] = raw
        else:
            raise ValueError("List attributes require explicit verified annotation.")
    return result


def normalize_supplier_price(
    filename: str, content: bytes, mapping: ConfirmedSupplierMapping, *,
    limits: ImportLimits | None = None,
) -> CatalogImportResult:
    """Offline canonical parse, never a publish or an authorization decision."""
    digest = hashlib.sha256(content).hexdigest()
    if mapping.confirmed is not True:
        return _safe_result(filename, digest, _error("mapping_unconfirmed", "Не подтверждено."))
    if mapping.source_sha256 != digest:
        return _safe_result(filename, digest, _error(
            "source_hash_mismatch", "Исходный файл изменился после сопоставления.",
        ))
    if mapping.category not in CATEGORY_ATTRIBUTES:
        return _safe_result(filename, digest, _error(
            "unsupported_category", "Нет типизированной таксономии для категории.",
        ))

    limits = limits or ImportLimits()
    preview = preview_supplier_price(
        filename, content, encoding_hint=mapping.encoding_hint,
        sheet_name=mapping.sheet_name, limits=limits,
    )
    if preview.errors:
        return CatalogImportResult(filename, digest, (), preview.errors)
    if preview.header_row is None:
        return _safe_result(filename, digest, _error("invalid_header", "Нет шапки."))
    try:
        rows, non_text = _rows_for_conversion(filename, content, mapping, limits)
    except _ImportRejected as error:
        return _safe_result(filename, digest, error.error)

    header_index = preview.header_row - 1
    headers = rows[header_index]
    validation_error = _approved_mapping_error(mapping, headers)
    if validation_error is not None:
        return _safe_result(filename, digest, validation_error)
    header_indices = {name: index for index, name in enumerate(headers)}
    sku_index = header_indices[mapping.columns["sku"]]
    products = []
    errors: list[ImportErrorDetail] = []
    seen_skus: set[str] = set()

    for row_number, row in enumerate(rows[header_index + 1:], start=preview.header_row + 1):
        if (row_number, sku_index) in non_text:
            errors.append(_error(
                "sku_not_text", "SKU в XLSX должен быть текстовым.", row_number, "sku",
            ))
            continue
        data: dict[str, str | int] = dict(mapping.constants)
        for field_name, column_name in mapping.columns.items():
            data[field_name] = row[header_indices[column_name]].strip()
        data["category"] = mapping.category
        if "sale_unit" in mapping.columns:
            raw_unit = data["sale_unit"]
            if not isinstance(raw_unit, str) or raw_unit not in mapping.unit_values:
                errors.append(_error(
                    "unmapped_unit", "Единица измерения требует подтверждения.",
                    row_number, "sale_unit",
                ))
                continue
            data["sale_unit"] = mapping.unit_values[raw_unit]
        try:
            attributes = _attributes(row, header_indices, mapping)
            canonical_values: list[object] = [
                json.dumps(attributes, ensure_ascii=False)
                if name == "attributes_json" else data.get(name, "")
                for name in COLUMNS_V2
            ]
            product = _parse_row(canonical_values, row_number, False, COLUMNS_V2)
        except _ImportRejected as error:
            errors.append(error.error)
            continue
        except (TypeError, ValueError):
            errors.append(_error(
                "invalid_product", "Некорректные типизированные атрибуты.",
                row_number, "attributes",
            ))
            continue
        if product.sku in seen_skus:
            errors.append(_error("duplicate_sku", "Повторяющийся SKU.", row_number, "sku"))
            continue
        seen_skus.add(product.sku)
        products.append(product)
    return CatalogImportResult(
        filename, digest, tuple(products) if not errors else (), tuple(errors),
    )
