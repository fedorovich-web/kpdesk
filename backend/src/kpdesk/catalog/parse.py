"""CSV/XLSX validation with explicit format and decompression boundaries."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Iterable, Iterator, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import PurePath
from zipfile import BadZipFile, ZipFile

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from openpyxl import load_workbook
from openpyxl.cell.read_only import ReadOnlyCell
from pydantic import ValidationError

from kpdesk.catalog.schema import (
    COLUMNS,
    COLUMNS_V2,
    CatalogImportResult,
    ImportErrorDetail,
    ImportLimits,
)
from kpdesk.matching.schema import CatalogProduct

_DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?\Z")
_INTEGER = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_BAD_XLSX_MEMBERS = ("externallinks/", "vbaproject", "activex/", "embeddings/", "connections.xml")


class _ImportRejected(Exception):
    def __init__(self, code: str, message: str, row: int | None = None) -> None:
        self.error = ImportErrorDetail(code, message, row)
        super().__init__(message)


def _safe_rows_csv(content: bytes, limits: ImportLimits) -> Iterator[tuple[int, list[object]]]:
    if b"\x00" in content or content.startswith(b"PK\x03\x04"):
        raise _ImportRejected("invalid_format", "Ожидался текстовый CSV.")
    try:
        decoded = content.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as error:
        raise _ImportRejected("invalid_encoding", "Требуется UTF-8 CSV.") from error
    try:
        for index, row in enumerate(
            csv.reader(io.StringIO(decoded, newline=""), delimiter=";", strict=True),
            start=1,
        ):
            if index > limits.max_rows + 1:
                raise _ImportRejected("too_many_rows", "Слишком много строк.", index)
            yield index, list(row)
    except (csv.Error, UnicodeError) as error:
        raise _ImportRejected("invalid_csv", "Некорректная структура CSV.") from error


def _check_archive_member_name(name: str) -> None:
    if "\\" in name or name.startswith("/") or any(
        segment in ("", ".", "..") for segment in name.split("/")
    ):
        raise _ImportRejected("invalid_archive", "Недопустимый путь внутри XLSX.")


def _validate_xlsx_zip(content: bytes, limits: ImportLimits) -> None:
    if not content.startswith(b"PK\x03\x04"):
        raise _ImportRejected("invalid_format", "Ожидался ZIP-контейнер XLSX.")
    try:
        with ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
            if len(members) > limits.max_archive_entries:
                raise _ImportRejected("archive_limits", "Слишком много файлов XLSX.")
            names: set[str] = set()
            total_uncompressed = 0
            for member in members:
                name = member.filename
                _check_archive_member_name(name)
                canonical = name.casefold()
                if canonical in names:
                    raise _ImportRejected("invalid_archive", "Дублирующиеся файлы XLSX.")
                names.add(canonical)
                if any(part in canonical for part in _BAD_XLSX_MEMBERS):
                    raise _ImportRejected("unsafe_xlsx", "Внешние связи или активное содержимое.")
                if member.flag_bits & 0x1:
                    raise _ImportRejected("unsafe_xlsx", "Зашифрованный XLSX не поддерживается.")
                total_uncompressed += member.file_size
                if (total_uncompressed > limits.max_uncompressed_bytes
                    or member.file_size > limits.max_uncompressed_bytes
                    or member.file_size > member.compress_size * limits.max_compression_ratio
                ):
                    raise _ImportRejected("archive_limits", "Превышен лимит распаковки XLSX.")
                if canonical.endswith((".xml", ".rels")):
                    if member.file_size > limits.max_xml_member_bytes:
                        raise _ImportRejected("archive_limits", "XML XLSX слишком большой.")
                    data = archive.read(member)
                    try:
                        root = ElementTree.fromstring(data, forbid_dtd=True)
                    except (DefusedXmlException, ElementTree.ParseError) as error:
                        raise _ImportRejected("unsafe_xml", "Некорректный или небезопасный XML.") from error
                    if canonical.endswith(".rels"):
                        for element in root.iter():
                            if element.attrib.get("TargetMode", "").casefold() == "external":
                                raise _ImportRejected("unsafe_xlsx", "Внешние URL запрещены.")
            if not {"[content_types].xml", "xl/workbook.xml"}.issubset(names):
                raise _ImportRejected("invalid_format", "Нет структуры рабочей книги XLSX.")
    except (BadZipFile, OSError, EOFError, RuntimeError) as error:
        raise _ImportRejected("invalid_archive", "Повреждённый XLSX.") from error


def _safe_rows_xlsx(content: bytes, limits: ImportLimits) -> Iterator[tuple[int, list[object]]]:
    _validate_xlsx_zip(content, limits)
    try:
        wb = load_workbook(
            io.BytesIO(content), read_only=True, data_only=False,
            keep_links=False, keep_vba=False,
        )
        try:
            if wb.sheetnames != ["Products"]:
                raise _ImportRejected("invalid_sheet", "Нужен один лист Products.", 1)
            ws = wb["Products"]
            if (ws.max_row is not None and ws.max_row > limits.max_rows + 1):
                raise _ImportRejected("too_many_rows", "Превышено число строк XLSX.")
            if (ws.max_column is not None and ws.max_column > len(COLUMNS_V2)):
                raise _ImportRejected("invalid_header", "Лишние столбцы XLSX.", 1)
            for row_number, cells in enumerate(
                ws.iter_rows(max_row=limits.max_rows + 2, max_col=ws.max_column or len(COLUMNS_V2)),
                start=1,
            ):
                if row_number > limits.max_rows + 1:
                    raise _ImportRejected("too_many_rows", "Превышено число строк XLSX.", row_number)
                for cell in cells:
                    if isinstance(cell, ReadOnlyCell) and cell.data_type == "f":
                        raise _ImportRejected(
                            "formula_forbidden", "Формулы в прайсе запрещены.", row_number
                        )
                yield row_number, [
                    cell.value if isinstance(cell, ReadOnlyCell) else None for cell in cells
                ]
        finally:
            wb.close()
    except _ImportRejected:
        raise
    except (BadZipFile, OSError, ValueError, TypeError, KeyError) as error:
        raise _ImportRejected("invalid_xlsx", "Некорректная книга XLSX.") from error


def _parse_positive_integer(raw: object) -> int:
    if isinstance(raw, bool):
        raise TypeError("Ожидалось положительное целое число.")
    value = str(raw).strip()
    if not _INTEGER.fullmatch(value) or int(value) < 1:
        raise ValueError("Ожидалось положительное целое число.")
    return int(value)


def _parse_price(raw: object) -> Decimal:
    value = str(raw).strip()
    if not _DECIMAL.fullmatch(value):
        raise ValueError("Цена должна быть десятичным числом с точкой и максимум двумя знаками.")
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("Некорректная цена.") from error
    if not result.is_finite():
        raise ValueError("Некорректная цена.")
    return result


def _unique_attributes(pairs: list[tuple[str, object]]) -> dict[str, object]:
    attributes: dict[str, object] = {}
    for key, value in pairs:
        if key in attributes:
            raise ValueError("Повторяющиеся ключи в attributes_json запрещены.")
        attributes[key] = value
    return attributes


def _parse_row(
    values: Sequence[object], row: int, from_excel: bool,
    columns: tuple[str, ...],
) -> CatalogProduct:
    if from_excel and not isinstance(values[0], str):
        raise _ImportRejected("sku_not_text", "SKU в XLSX должен быть текстом.", row)
    if len(values) != len(columns):
        raise _ImportRejected("invalid_row_width", "Неверное число столбцов.", row)
    data: dict[str, object] = {}
    for name, value in zip(columns, values, strict=True):
        if isinstance(value, bool) or (name not in (
            "price_rub", "pieces_per_sale_unit", "paper_gsm"
        ) and not isinstance(value, (str, type(None)))):
            raise _ImportRejected("invalid_product", f"Неверный тип ячейки {name}.", row)
        data[name] = value.strip() if isinstance(value, str) else value
    sku = data["sku"]
    if not isinstance(sku, str) or not sku.strip():
        raise _ImportRejected("invalid_product", "SKU обязателен.", row)
    try:
        data["pieces_per_sale_unit"] = _parse_positive_integer(data["pieces_per_sale_unit"])
        try:
            data["price_rub"] = _parse_price(data["price_rub"])
        except ValueError as error:
            raise _ImportRejected("invalid_price", str(error), row) from error
        data["paper_gsm"] = (
            _parse_positive_integer(data["paper_gsm"])
            if data["paper_gsm"] not in ("", None)
            else None
        )
        for optional in (
            "brand", "paper_format", "pen_ink_color", "pen_ink_type"
        ):
            if data[optional] == "":
                data[optional] = None
        attributes_json = data.pop("attributes_json", "")
        if not attributes_json:
            data["attributes"] = {}
        else:
            if not isinstance(attributes_json, str):
                raise ValueError("attributes_json должен быть текстовым JSON.")
            try:
                parsed_attributes = json.loads(
                    attributes_json, object_pairs_hook=_unique_attributes,
                )
            except json.JSONDecodeError as error:
                raise ValueError("Некорректный attributes_json.") from error
            if not isinstance(parsed_attributes, dict):
                raise ValueError("attributes_json должен содержать объект.")
            data["attributes"] = parsed_attributes
        product = CatalogProduct.model_validate(data)
        if product.sale_unit == "штука" and product.pieces_per_sale_unit != 1:
            raise ValueError("Для единицы продажи «штука» количество штук в единице должно быть 1.")
        if product.category == "paper":
            if (not product.paper_format or not product.paper_gsm
                or product.pen_ink_color or product.pen_ink_type
                or product.sale_unit == "штука"
                or (product.sale_unit == "пачка" and product.pieces_per_sale_unit != 1)
            ):
                raise ValueError("Недопустимые характеристики или упаковка бумаги.")
        elif product.category == "pen":
            if (not product.pen_ink_color or not product.pen_ink_type
                or product.paper_format or product.paper_gsm is not None
                or (product.sale_unit == "штука" and product.pieces_per_sale_unit != 1)):
                raise ValueError("Недопустимые характеристики или упаковка ручки.")
        elif (product.paper_format or product.paper_gsm is not None
              or product.pen_ink_color or product.pen_ink_type):
            raise ValueError("Характеристики бумаги и ручек запрещены для других категорий.")
        return product
    except (TypeError, ValueError, ValidationError) as error:
        raise _ImportRejected(
            "invalid_product",
            "Данные товара не прошли проверку. Проверьте название, категорию, единицу продажи, упаковку и характеристики."
            if isinstance(error, (TypeError, ValidationError)) else str(error),
            row,
        ) from error


def parse_price_list(
    filename: str,
    content: bytes,
    limits: ImportLimits,
) -> CatalogImportResult:
    """Parse an entire catalog or return row-specific errors with no publishable products."""
    digest = hashlib.sha256(content).hexdigest()
    errors: list[ImportErrorDetail] = []
    products: list[CatalogProduct] = []
    if not content or len(content) > limits.max_upload_bytes:
        return CatalogImportResult(filename, digest, (), (
            ImportErrorDetail("file_too_large", "Файл пуст или превышает лимит."),
        ))
    suffix = PurePath(filename).suffix.casefold()
    if suffix not in (".csv", ".xlsx"):
        return CatalogImportResult(filename, digest, (), (
            ImportErrorDetail("invalid_extension", "Разрешены только CSV и XLSX."),
        ))

    seen_skus: set[str] = set()
    try:
        lines: Iterable[tuple[int, list[object]]] = (
            _safe_rows_csv(content, limits)
            if suffix == ".csv"
            else _safe_rows_xlsx(content, limits)
        )
        header_seen = False
        columns = COLUMNS
        for row_number, values in lines:
            row_too_long = False
            for name, cell in zip(COLUMNS_V2, values, strict=False):
                if cell is not None and len(str(cell)) > limits.max_cell_chars:
                    row_too_long = True
                    errors.append(ImportErrorDetail(
                        "cell_too_long", "Превышена длина ячейки.", row_number, name,
                    ))
            if len(values) > len(COLUMNS_V2) and any(v is not None for v in values[len(COLUMNS_V2):]):
                errors.append(ImportErrorDetail(
                    "invalid_row_width", "Лишние ячейки.", row_number
                ))
            if row_number == 1:
                header_seen = True
                header = tuple(values)
                if header == COLUMNS:
                    columns = COLUMNS
                elif header == COLUMNS_V2:
                    columns = COLUMNS_V2
                else:
                    errors.append(ImportErrorDetail(
                        "invalid_header", "Заголовки не соответствуют шаблону.", 1
                    ))
                    break
                continue
            if row_too_long:
                continue
            if not any(value not in ("", None) for value in values):
                errors.append(ImportErrorDetail(
                    "empty_row", "Пустая строка внутри прайса.", row_number
                ))
                continue
            try:
                product = _parse_row(values, row_number, suffix == ".xlsx", columns)
                if product.sku in seen_skus:
                    errors.append(ImportErrorDetail(
                        "duplicate_sku", "Дублирующийся SKU.", row_number, "sku"
                    ))
                else:
                    seen_skus.add(product.sku)
                    products.append(product)
            except _ImportRejected as error:
                errors.append(error.error)
        if not header_seen:
            errors.append(ImportErrorDetail("invalid_header", "Отсутствуют заголовки.", 1))
        if not products and not errors:
            errors.append(ImportErrorDetail("empty_catalog", "Нет товаров.", 2))
    except _ImportRejected as error:
        errors.append(error.error)
    return CatalogImportResult(
        filename,
        digest,
        tuple(products) if not errors else (),
        tuple(errors),
    )
