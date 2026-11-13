"""JSON product-array adapter. Converts explicit JSON field paths to the same safe staging path.

Supports a root array or a single named top-level list; no network, DB,
product publication, pseudo-SKU generation or implicit category conversion.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import PurePath

from kpdesk.catalog.schema import ImportLimits
from kpdesk.catalog.supplier_staging import (
    SupplierMapping,
    SupplierStaging,
    _stage_source_rows,
)

_ALLOWED_LIST_KEYS = frozenset(("items", "products", "data", "offers"))

@dataclass(frozen=True, slots=True)
class JsonSupplierMapping:
    name: str
    price: str
    sku: str | None = None
    currency: str | None = None
    category: str | None = None
    sale_unit: str | None = None
    pieces_per_sale_unit: str | None = None
    items_key: str | None = None
    confirmed_currency: str | None = None

    def __post_init__(self) -> None:
        props = (
            self.name, self.price, self.sku, self.currency,
            self.category, self.sale_unit, self.pieces_per_sale_unit,
        )
        mapped = [key for key in props if key is not None]
        if (
            any(not isinstance(value, str) or not value or len(value) > 100
                or "." in value or "[" in value or "]" in value for value in mapped)
            or len(mapped) != len(set(mapped))
            or self.items_key not in (*_ALLOWED_LIST_KEYS, None)
            or self.confirmed_currency not in ("RUB", None)
        ):
            raise ValueError("Invalid JSON supplier field mapping.")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    obj: dict[str, object] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("Duplicate JSON object key.")
        obj[key] = value
    return obj


def _invalid_constant(_: str) -> None:
    raise ValueError("Non-finite JSON numeric values are forbidden.")


def _scalar(value: object) -> object:
    if value is None or type(value) in (str, int, Decimal):
        return value
    raise ValueError("Nested, boolean or unsupported JSON product field.")


def stage_json_supplier_prices(
    filename: str,
    content: bytes,
    mapping: JsonSupplierMapping,
    *,
    limits: ImportLimits | None = None,
) -> SupplierStaging:
    limits = limits or ImportLimits()
    if PurePath(filename).suffix.lower() != ".json":
        raise ValueError("Expected .json extension.")
    if not content or len(content) > limits.max_upload_bytes or b"\x00" in content:
        raise ValueError("Empty, binary or oversized JSON supplier file.")
    try:
        root: object = json.loads(
            content.decode("utf-8-sig", errors="strict"),
            parse_float=Decimal,
            parse_constant=_invalid_constant,
            object_pairs_hook=_unique_object,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("Invalid supplier JSON document.") from error
    items: object
    if mapping.items_key is None:
        if not isinstance(root, list):
            raise ValueError("JSON supplier root must be an array.")
        items = root
    else:
        if not isinstance(root, dict) or set(root) != {mapping.items_key}:
            raise ValueError("JSON root must contain only selected product array.")
        items = root.get(mapping.items_key)
    if not isinstance(items, list) or not 1 <= len(items) <= limits.max_rows:
        raise ValueError("JSON product array empty or exceeds row limit.")
    keys = (
        mapping.name, mapping.price, mapping.sku, mapping.currency,
        mapping.category, mapping.sale_unit, mapping.pieces_per_sale_unit,
    )
    selected = [key for key in keys if key is not None]
    if len(selected) > 64:
        raise ValueError("Too many mapped properties.")
    rows: list[tuple[int, list[object]]] = [(1, [str(name) for name in selected])]
    for line, item in enumerate(items, 2):
        if not isinstance(item, dict) or len(item) > 64:
            raise ValueError(f"Invalid JSON product object at row {line}.")
        if any(not isinstance(key, str) or len(key) > 100 for key in item):
            raise ValueError(f"Invalid JSON property name at row {line}.")
        values = [_scalar(item.get(key)) for key in selected]
        if any(len(str(value)) > limits.max_cell_chars for value in values
               if value is not None):
            raise ValueError(f"JSON product field limit exceeded at row {line}.")
        rows.append((line, values))
    positions = {name: index for index, name in enumerate(selected)}
    adapted = SupplierMapping(
        name=positions[mapping.name],
        price=positions[mapping.price],
        sku=positions[mapping.sku] if mapping.sku else None,
        currency=positions[mapping.currency] if mapping.currency else None,
        category=positions[mapping.category] if mapping.category else None,
        sale_unit=positions[mapping.sale_unit] if mapping.sale_unit else None,
        pieces_per_sale_unit=positions[mapping.pieces_per_sale_unit]
        if mapping.pieces_per_sale_unit else None,
        header_row=1,
        confirmed_currency="RUB" if mapping.confirmed_currency == "RUB" else None,
    )
    return _stage_source_rows(filename, content, adapted, rows, limits)
