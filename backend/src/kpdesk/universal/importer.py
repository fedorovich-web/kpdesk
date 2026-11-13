"""Canonical CSV/XLSX importer, atomic validation and source provenance."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePath
from uuid import uuid4

from pydantic import ValidationError

from kpdesk.catalog.parse import _ImportRejected, _safe_rows_csv
from kpdesk.catalog.schema import ImportLimits
from kpdesk.catalog.supplier_preview import _xlsx_rows
from kpdesk.llm.client import unique_object
from kpdesk.universal.schema import CatalogSnapshot, Specification, UniversalProduct
from kpdesk.universal.units import normalized

_REQUIRED = {"sku", "name", "price_rub", "sale_unit"}
_OPTIONAL = {"category", "brand", "model", "price_quantity", "quantity_step", "specifications_json"}
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")


class UniversalImportError(ValueError):
    def __init__(self, code: str, row: int | None = None) -> None:
        self.code, self.row = code, row
        super().__init__(code)


def parse_universal_price(
    filename: str, content: bytes, limits: ImportLimits | None = None,
) -> CatalogSnapshot:
    bounds = limits or ImportLimits()
    if not content or len(content) > bounds.max_upload_bytes:
        raise UniversalImportError("upload_limits")
    suffix = PurePath(filename).suffix.casefold()
    non_text_cells: frozenset[tuple[int, int]] = frozenset()
    try:
        if suffix == ".csv":
            rows = [(number, ["" if cell is None else str(cell) for cell in cells])
                    for number, cells in _safe_rows_csv(content, bounds)]
        elif suffix == ".xlsx":
            values, _, non_text_cells = _xlsx_rows(content, "Products", bounds)
            rows = list(enumerate(values, 1))
        else:
            raise UniversalImportError("unsupported_format")
    except _ImportRejected as error:
        raise UniversalImportError(error.error.code, error.error.row) from error
    if not rows or len(rows) > min(bounds.max_rows, 5000) + 1:
        raise UniversalImportError("row_limits")
    header = [cell.strip() for cell in rows[0][1]]
    keys = [normalized(cell) for cell in header]
    if (not _REQUIRED.issubset(header) or len(header) > 64 or any(not cell for cell in header)
        or len(keys) != len(set(keys))):
        raise UniversalImportError("invalid_header", 1)
    identity_columns = {index for index, name in enumerate(header)
                        if name in {"sku", "name", "brand", "model", "category", "sale_unit"}}
    if any(number == 1 or column in identity_columns for number, column in non_text_cells):
        raise UniversalImportError("non_text_identity")
    products: list[UniversalProduct] = []
    for number, cells in rows[1:]:
        if not any(cell.strip() for cell in cells):
            continue
        if len(cells) > len(header):
            raise UniversalImportError("invalid_row", number)
        cells += [""] * (len(header) - len(cells))
        if any(len(cell) > bounds.max_cell_chars for cell in cells):
            raise UniversalImportError("cell_limits", number)
        row = dict(zip(header, (cell.strip() for cell in cells), strict=True))
        fields: dict[str, object] = {name: row[name] for name in _REQUIRED}
        for name in _OPTIONAL - {"specifications_json"}:
            if row.get(name):
                fields[name] = row[name]
        for name in ("price_rub", "price_quantity", "quantity_step"):
            value = fields.get(name)
            if value is not None and (not isinstance(value, str) or not _DECIMAL.fullmatch(value)):
                raise UniversalImportError("invalid_decimal", number)
        try:
            specs: list[Specification] = []
            if row.get("specifications_json"):
                raw = json.loads(row["specifications_json"], object_pairs_hook=unique_object)
                if not isinstance(raw, list):
                    raise ValueError("specification_array_required")
                specs.extend(Specification.model_validate(item) for item in raw)
            specs.extend(Specification(name=name, value=value) for name, value in row.items()
                         if name not in _REQUIRED | _OPTIONAL and value)
            fields.update(specifications=tuple(specs), source_row=number)
            products.append(UniversalProduct.model_validate(fields))
        except (ValueError, TypeError) as error:
            raise UniversalImportError("invalid_product", number) from error
    try:
        return CatalogSnapshot(version_id=uuid4(), source_sha256=hashlib.sha256(content).hexdigest(),
                               products=tuple(products))
    except ValidationError as error:
        raise UniversalImportError("invalid_catalog") from error
