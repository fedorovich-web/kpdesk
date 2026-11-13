"""Safe, format-agnostic first-stage supplier price intake.

All transformations require an explicit, operator-reviewed column mapping.
This preserves evidence from arbitrary vendor CSV/XLSX layouts; it is NOT a
catalog publication or proof of pricing, availability, or ML training rights.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import PurePath
from typing import Literal

import xlrd
from openpyxl import load_workbook
from openpyxl.cell.read_only import ReadOnlyCell

from kpdesk.catalog.parse import _ImportRejected, _validate_xlsx_zip
from kpdesk.catalog.schema import ImportLimits

_PRICE = re.compile(r"(?:[0-9]+|[1-9][0-9]{0,2}(?:[ \u00a0\u202f][0-9]{3})+)(?:[.,][0-9]{1,2})?\Z")
_HIGH_PRECISION_PRICE = re.compile(r"(?:[0-9]+|[1-9][0-9]{0,2}(?:[ \u00a0\u202f][0-9]{3})+)[.,][0-9]{3,24}\Z")
_CURRENCIES = frozenset(("RUB", "руб.", "руб", "₽"))
_INLINE_RUB = re.compile(r"\s*(?:руб\.?|₽|RUB)\s*\Z", re.IGNORECASE)
_DELIMITERS = frozenset((";", ",", "\t"))
_ENCODINGS = frozenset(("utf-8", "utf-8-sig", "cp1251"))


@dataclass(frozen=True, slots=True)
class SupplierMapping:
    """Zero-based physical columns and one-based header row. No guessed category."""

    name: int
    price: int
    header_row: int
    sku: int | None = None
    currency: int | None = None
    category: int | None = None
    sale_unit: int | None = None
    pieces_per_sale_unit: int | None = None
    # A human explicitly confirms the exact supplier's currency if not in a column.
    confirmed_currency: Literal["RUB"] | None = None
    sheet: str | None = None
    encoding: Literal["utf-8", "utf-8-sig", "cp1251"] = "utf-8-sig"
    delimiter: Literal[";", ",", "\t"] = ";"

    def __post_init__(self) -> None:
        columns = (
            self.name, self.price, self.sku, self.currency, self.category,
            self.sale_unit, self.pieces_per_sale_unit,
        )
        positions = [index for index in columns if index is not None]
        if (
            not 1 <= self.header_row <= 20
            or any(not isinstance(x, int) or isinstance(x, bool)
                   or not 0 <= x < 64 for x in positions)
            or len(positions) != len(set(positions))
            or self.delimiter not in _DELIMITERS
            or self.encoding not in _ENCODINGS
            or self.confirmed_currency not in (None, "RUB")
        ):
            raise ValueError("Invalid, duplicated or unsafe supplier mapping.")


def mapping_digest(mapping: SupplierMapping, formula_policy: str) -> str:
    """Deterministic digest of exactly the source mapping and formula policy."""
    if formula_policy not in ("reject", "quarantine_rows"):
        raise ValueError("Unsupported formula policy.")
    packed = json.dumps(
        {"mapping": asdict(mapping), "formula_policy": formula_policy},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(packed.encode("utf-8")).hexdigest()


PACK_SIZE_REVIEW_MESSAGES = {
    "pack_size_requires_review": (
        "Столбец размера упаковки не выбран. Сверьте фасовку и единицу цены с оригиналом."
    ),
    "missing_pack_size": (
        "Столбец размера упаковки выбран, но значение в строке пустое. "
        "Подтвердите единицу цены по оригиналу поставщика."
    ),
    "invalid_pack_size": (
        "Некорректный размер упаковки: допустимо только целое число от 1 до 100000. "
        "Проверьте исходное значение; формульные строки остаются в карантине."
    ),
}


def pack_size_review_reason(issues: tuple[str, ...]) -> str | None:
    """Localized operator guidance for one source-bound package issue."""
    return next(
        (PACK_SIZE_REVIEW_MESSAGES[code] for code in issues if code in PACK_SIZE_REVIEW_MESSAGES),
        None,
    )


@dataclass(frozen=True, slots=True)
class StagedSupplierRow:
    line: int
    supplier_sku: str | None
    name: str
    price_rub: Decimal | None
    source_category: str | None
    source_sale_unit: str | None
    source_pack_size: int | None
    issues: tuple[str, ...]
    formula_columns: tuple[int, ...] = ()
    source_high_precision_price: str | None = None


@dataclass(frozen=True, slots=True)
class SupplierStaging:
    sha256: str
    source_filename: str
    rows: tuple[StagedSupplierRow, ...]
    # Never publish raw stages. Canonical category/unit attributes need review.
    canonical_importable: bool = False
    model_training_authorized: bool = False


def _as_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else str(value) if value is not None else ""


def _source_price_decimal(value: object, pattern: re.Pattern[str]) -> Decimal | None:
    """Parse a source amount, never apply rounding or currency conversion."""
    text_value = _INLINE_RUB.sub("", _as_text(value)).strip()
    if isinstance(value, bool) or not pattern.fullmatch(text_value):
        return None
    try:
        amount = Decimal(
            text_value.replace(" ", "").replace("\u00a0", "")
            .replace("\u202f", "").replace(",", ".")
        )
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and Decimal(0) < amount < Decimal(1000000000000) else None


def _price_rub(value: object) -> Decimal | None:
    return _source_price_decimal(value, _PRICE)


def _source_high_precision_amount(value: object) -> Decimal | None:
    """Source amount needing an explicit money precision review (3–24 decimals)."""
    return _source_price_decimal(value, _HIGH_PRECISION_PRICE)


def _source_pack_quantity(value: object) -> int | None:
    """Keep integer Excel cell semantics; refuse ambiguous textual fractions."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 100_000 else None
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        return int(value) if 1 <= value <= 100_000 else None
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdecimal():
            number = int(stripped)
            return number if 1 <= number <= 100_000 else None
    return None


@dataclass(frozen=True, slots=True)
class _QuarantinedFormula:
    """Formula cell marker, never execute or convert its cached value."""

    column: int


def _source_rows(
    filename: str, content: bytes, mapping: SupplierMapping, limits: ImportLimits,
    *, formula_policy: Literal["reject", "quarantine_rows"] = "reject",
) -> list[tuple[int, list[object]]]:
    suffix = PurePath(filename).suffix.casefold()
    if not content or len(content) > limits.max_upload_bytes:
        raise ValueError("Empty or oversized supplier source.")
    if suffix == ".csv":
        if b"\x00" in content or content.startswith(b"PK\x03\x04"):
            raise ValueError("Supplier CSV is not text.")
        try:
            csv_text = content.decode(mapping.encoding, errors="strict")
            lines = list(csv.reader(
                io.StringIO(csv_text, newline=""), delimiter=mapping.delimiter, strict=True,
            ))
        except (UnicodeError, csv.Error) as error:
            raise ValueError("Supplier CSV decoding or structure failed.") from error
        if len(lines) > limits.max_rows + mapping.header_row:
            raise ValueError("Supplier CSV row limit exceeded.")
        if any(len(row) > 64 for row in lines):
            raise ValueError("Supplier CSV column limit exceeded.")
        return [(index, list(row)) for index, row in enumerate(lines, 1)]
    if suffix == ".xls":
        if not content.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
            raise ValueError("Invalid legacy XLS signature.")
        try:
            workbook_xls = xlrd.open_workbook(
                file_contents=content, on_demand=True, ragged_rows=True,
            )
            try:
                if mapping.sheet is None or mapping.sheet not in workbook_xls.sheet_names():
                    raise ValueError("Exact source XLS sheet must be selected.")
                sheet_xls = workbook_xls.sheet_by_name(mapping.sheet)
                if sheet_xls.nrows > limits.max_rows + mapping.header_row or sheet_xls.ncols > 64:
                    raise ValueError("Supplier XLS limits exceeded.")
                xls_output: list[tuple[int, list[object]]] = []
                for row_index in range(sheet_xls.nrows):
                    xls_cells = sheet_xls.row(row_index)
                    xls_row: list[object] = []
                    for xls_cell in xls_cells:
                        if xls_cell.ctype in (xlrd.XL_CELL_DATE, xlrd.XL_CELL_ERROR):
                            raise ValueError("Unsupported XLS date/error cell.")
                        xls_row.append(xls_cell.value if xls_cell.ctype != xlrd.XL_CELL_EMPTY else None)
                    xls_output.append((row_index + 1, xls_row))
                return xls_output
            finally:
                workbook_xls.release_resources()
        except xlrd.XLRDError as error:  # type: ignore[attr-defined]
            raise ValueError("Corrupted or unsupported legacy XLS.") from error
    if suffix != ".xlsx":
        raise ValueError("Supported tabular files: CSV, XLS or XLSX.")
    try:
        _validate_xlsx_zip(content, limits)
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=False,
                                 keep_links=False)
        try:
            if mapping.sheet is None or mapping.sheet not in workbook.sheetnames:
                raise ValueError("Exact source XLSX sheet must be selected.")
            page = workbook[mapping.sheet]
            if (
                page.max_row is not None
                and page.max_row > limits.max_rows + mapping.header_row
            ) or (page.max_column is not None and page.max_column > 64):
                raise ValueError("Supplier XLSX row/column limit exceeded.")
            output: list[tuple[int, list[object]]] = []
            for line, cells in enumerate(
                page.iter_rows(max_row=limits.max_rows + mapping.header_row, max_col=64), 1,
            ):
                row: list[object] = []
                for cell in cells:
                    if isinstance(cell, ReadOnlyCell) and cell.data_type == "f":
                        if formula_policy == "reject":
                            raise ValueError("Supplier XLSX formula rejected.")
                        row.append(_QuarantinedFormula(cell.column - 1))
                        continue
                    row.append(cell.value if isinstance(cell, ReadOnlyCell) else None)
                output.append((line, row))
            return output
        finally:
            workbook.close()
    except _ImportRejected as error:
        raise ValueError("Unsafe supplier XLSX archive.") from error


def stage_supplier_prices(
    filename: str,
    content: bytes,
    mapping: SupplierMapping,
    *,
    limits: ImportLimits | None = None,
    formula_policy: Literal["reject", "quarantine_rows"] = "reject",
) -> SupplierStaging:
    """Retain all rows, including incomplete lines, with machine-readable issues.

    Explicit field mapping is not an authorization to publish, train or guess
    missing data. No database writes or network requests happen here.
    """
    if formula_policy not in ("reject", "quarantine_rows"):
        raise ValueError("Unsupported formula_policy.")
    limits = limits or ImportLimits()
    source = _source_rows(
        filename, content, mapping, limits, formula_policy=formula_policy,
    )
    return _stage_source_rows(filename, content, mapping, source, limits)


def _stage_source_rows(
    filename: str,
    content: bytes,
    mapping: SupplierMapping,
    source: list[tuple[int, list[object]]],
    limits: ImportLimits,
) -> SupplierStaging:
    """Shared audited normalization for delimited, Excel and object-based sources."""
    if len(source) <= mapping.header_row:
        raise ValueError("Supplier source has no product data.")
    selected = (
        mapping.name, mapping.price, mapping.sku, mapping.currency,
        mapping.category, mapping.sale_unit, mapping.pieces_per_sale_unit,
    )
    highest = max(position for position in selected if position is not None)
    results: list[StagedSupplierRow] = []
    for line, source_row in source[mapping.header_row:]:
        formula_columns = tuple(
            index for index, item in enumerate(source_row)
            if isinstance(item, _QuarantinedFormula)
        )
        row = [
            None if isinstance(item, _QuarantinedFormula) else item
            for item in source_row
        ]
        if not any(_as_text(x) for x in row) and not formula_columns:
            continue
        missing_mapped_columns = len(row) <= highest
        if missing_mapped_columns:
            # Ragged supplier exports may contain section labels or truncated
            # rows. Preserve the original line as review-only evidence rather
            # than rejecting every otherwise valid product in the upload.
            row.extend([None] * (highest + 1 - len(row)))
        if any(len(_as_text(x)) > limits.max_cell_chars for x in row):
            raise ValueError(f"Supplier source row {line} exceeds safe cell length.")
        issues: list[str] = []
        if missing_mapped_columns:
            issues.append("missing_mapped_columns")
        if formula_columns:
            issues.append("xlsx_formula_cell_quarantined")
        raw_name = _as_text(row[mapping.name])
        if not raw_name:
            issues.append("missing_name")
        sku = _as_text(row[mapping.sku]) if mapping.sku is not None else None
        if mapping.sku is None or not sku:
            issues.append("missing_supplier_sku")
            sku = None
        elif not isinstance(row[mapping.sku], str):
            issues.append("numeric_sku_may_lose_leading_zeros")
            sku = None
        currency = (
            _as_text(row[mapping.currency]) if mapping.currency is not None
            else mapping.confirmed_currency
            or ("RUB" if _INLINE_RUB.search(_as_text(row[mapping.price])) else None)
        )
        if currency not in _CURRENCIES:
            issues.append("unconfirmed_or_non_rub_currency")
        amount = _price_rub(row[mapping.price])
        precise_amount = (
            _source_high_precision_amount(row[mapping.price]) if amount is None else None
        )
        source_high_precision_price = (
            _as_text(row[mapping.price]) if precise_amount is not None else None
        )
        if source_high_precision_price is not None:
            issues.append("price_precision_requires_review")
        elif amount is None:
            issues.append("invalid_or_missing_price")
        if currency not in _CURRENCIES:
            amount = None
        category = _as_text(row[mapping.category]) if mapping.category is not None else ""
        if not category:
            issues.append("category_requires_review")
        unit = _as_text(row[mapping.sale_unit]) if mapping.sale_unit is not None else ""
        if not unit:
            issues.append("sale_unit_requires_review")
        pack: int | None = None
        if mapping.pieces_per_sale_unit is not None:
            original_pack = (
                source_row[mapping.pieces_per_sale_unit]
                if mapping.pieces_per_sale_unit < len(source_row) else None
            )
            pack = _source_pack_quantity(original_pack)
            if pack is None:
                issues.append(
                    "missing_pack_size" if original_pack is None
                    or isinstance(original_pack, str) and not original_pack.strip()
                    else "invalid_pack_size"
                )
        else:
            issues.append("pack_size_requires_review")
        results.append(StagedSupplierRow(
            line=line, supplier_sku=sku, name=raw_name, price_rub=amount,
            source_category=category or None, source_sale_unit=unit or None,
            source_pack_size=pack, issues=tuple(issues),
            formula_columns=formula_columns,
            source_high_precision_price=source_high_precision_price,
        ))
    if not results:
        raise ValueError("Supplier source has no nonempty product rows.")
    if PurePath(filename).suffix.casefold() == ".xls":
        # xlrd reads legacy cached cells; formula/macro provenance is not verified.
        results = [
            StagedSupplierRow(
                line=row.line, supplier_sku=row.supplier_sku, name=row.name,
                price_rub=row.price_rub, source_category=row.source_category,
                source_sale_unit=row.source_sale_unit, source_pack_size=row.source_pack_size,
                issues=(*row.issues, "xls_formula_and_macro_provenance_unverified"),
                source_high_precision_price=row.source_high_precision_price,
                formula_columns=row.formula_columns,
            )
            for row in results
        ]
    return SupplierStaging(
        sha256=hashlib.sha256(content).hexdigest(),
        source_filename=filename, rows=tuple(results),
    )
