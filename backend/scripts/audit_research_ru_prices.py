"""Offline audit of newly obtained Russian store originals; never authorizes ML training.

The existing six-source manifest remains immutable. This quarantined research
corpus accepts only explicitly pinned seller pages and source file URLs.
A catalog without original RUB prices cannot count as a price list.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[2] / "ml/data/real/research_candidate_prices"
MAX_BYTES = 8_000_000
SOURCES: dict[str, tuple[str, str, str, int]] = {
    "aqualiga_sanitary": (
        "https://aqualiga.ru/catalog/",
        "https://aqualiga.ru/upload/price.xlsx",
        "five_columns_two_sheets_row_currency",
        3894,
    ),
    "krepish_autofasteners": (
        "https://www.krepish40.ru/price/",
        "https://www.krepish40.ru/price/price.xlsx",
        "three_columns_empty_price",
        0,
    ),
}


def _validate_archive(path: Path) -> None:
    try:
        with ZipFile(path) as archive:
            entries = archive.infolist()
            names = {item.filename for item in entries}
            if not {"[Content_Types].xml", "xl/workbook.xml"} <= names:
                raise ValueError("Supplier source is not XLSX.")
            if len(entries) > 8000 or sum(item.file_size for item in entries) > 160_000_000:
                raise ValueError("Supplier XLSX archive exceeds safe limits.")
            if any(
                item.filename.startswith(("/", "\\"))
                or ".." in item.filename.split("/")
                or item.flag_bits & 1
                for item in entries
            ):
                raise ValueError("Unsafe or encrypted supplier XLSX archive.")
    except BadZipFile as error:
        raise ValueError("Invalid supplier XLSX archive.") from error


def _count_aqualiga(path: Path) -> int:
    book = load_workbook(path, read_only=True, data_only=False)
    try:
        if len(book.worksheets) != 2:
            raise ValueError("Aqualiga workbook sheet layout changed.")
        sheet = book.worksheets[0]
        if not 2 <= sheet.max_row <= 5000 or sheet.max_column != 5:
            raise ValueError("Aqualiga workbook shape changed.")
        if tuple(cell.value for cell in sheet[1][:3]) != (
            "Наименование товара", "Артикул", "Цена"
        ):
            raise ValueError("Aqualiga source columns changed.")
        if sheet["D1"].value != 'Валюта для цены "Цена"':
            raise ValueError("Aqualiga currency field missing.")
        count = 0
        for title, sku, price, currency, category in sheet.iter_rows(
            min_row=2, max_col=5, values_only=True
        ):
            if (
                not isinstance(title, str) or not title.strip()
                or not isinstance(sku, str) or not sku.strip()
            ):
                raise ValueError("Aqualiga supplier row missing source identity.")
            if currency != "RUB":
                raise ValueError("Aqualiga original source price currency not RUB.")
            if isinstance(price, bool) or not isinstance(price, (int, float)):
                raise TypeError("Aqualiga source price missing or not numeric.")
            try:
                amount = Decimal(str(price))
            except InvalidOperation as error:
                raise ValueError("Aqualiga source has invalid price.") from error
            if not amount.is_finite() or amount <= 0:
                raise ValueError("Aqualiga source has nonpositive price.")
            count += 1
        return count
    finally:
        book.close()


def _count_krepish(path: Path) -> int:
    book = load_workbook(path, read_only=True, data_only=False)
    try:
        if len(book.worksheets) != 1:
            raise ValueError("Krepish workbook layout changed.")
        sheet = book.worksheets[0]
        if not 2 <= sheet.max_row <= 5000 or sheet.max_column != 3:
            raise ValueError("Krepish workbook shape changed.")
        if tuple(cell.value for cell in sheet[1]) != (
            "Артикул", "Наименование", "Цена"
        ):
            raise ValueError("Krepish source columns changed.")
        for sku, title, price in sheet.iter_rows(min_row=2, max_col=3, values_only=True):
            if not isinstance(sku, str) or not sku.strip() or not isinstance(title, str):
                raise ValueError("Krepish source identity missing.")
            if price is not None:
                raise ValueError("Krepish workbook price column changed; re-audit required.")
        return 0  # Real catalog, but not an actual price list.
    finally:
        book.close()


def audit_research_ru_prices(root: Path = ROOT) -> list[dict[str, str | int | bool]]:
    manifest_file = root / "manifest.json"
    if manifest_file.is_symlink() or not manifest_file.is_file():
        raise ValueError("Supplier research manifest missing.")
    if manifest_file.stat().st_size > 100_000:
        raise ValueError("Supplier research manifest too large.")
    manifest: dict[str, Any] = json.loads(manifest_file.read_text(encoding="utf-8"))
    records = manifest.get("sources")
    if (
        manifest.get("schema_version") != "ru-rub-research-sources-v1"
        or manifest.get("training_approved") is not False
        or not isinstance(records, list)
        or len(records) != len(SOURCES)
    ):
        raise ValueError("Unapproved supplier source manifest.")
    seen: set[str] = set()
    output: list[dict[str, str | int | bool]] = []
    for record in records:
        if not isinstance(record, dict):
            raise TypeError("Malformed supplier source.")
        source_id = record.get("source_id")
        if not isinstance(source_id, str) or source_id not in SOURCES or source_id in seen:
            raise ValueError("Unrecognized or duplicated Russian supplier source.")
        seen.add(source_id)
        page, source_url, profile, expected = SOURCES[source_id]
        if (
            record.get("source_page") != page or record.get("source_file_url") != source_url
            or record.get("file") != source_id + ".xlsx"
            or record.get("format_profile") != profile
            or record.get("expected_candidate_rows") != expected
            or record.get("source_document_date") is not None
            or record.get("training_authorized") is not False
            or record.get("current_price_verified") is not False
            or record.get("canonical_importable") is not False
            or record.get("rights_status") != "requires_review"
        ):
            raise ValueError("Source host, format, provenance or approvals changed.")
        path = root / (source_id + ".xlsx")
        if path.is_symlink() or not path.is_file():
            raise ValueError("Source original missing or redirected.")
        raw_bytes = path.stat().st_size
        if not 100 <= raw_bytes <= MAX_BYTES or raw_bytes != record.get("bytes"):
            raise ValueError("Supplier original byte size changed.")
        if hashlib.sha256(path.read_bytes()).hexdigest() != record.get("sha256"):
            raise ValueError("Supplier original SHA256 changed.")
        _validate_archive(path)
        count = _count_aqualiga(path) if source_id == "aqualiga_sanitary" else _count_krepish(path)
        if count != expected:
            raise ValueError("Supplier source row-count evidence changed.")
        is_price = source_id == "aqualiga_sanitary"
        if (
            record.get("source_currency") != ("RUB" if is_price else "unverified_no_prices")
            or (not is_price and record.get("exclusion_reason") != "price_column_empty")
        ):
            raise ValueError("Supplier source currency or exclusion claim invalid.")
        output.append({
            "source_id": source_id,
            "format_profile": profile,
            "candidate_product_rows": count,
            "native_rub_verified": is_price,
            "exclusion_reason": "" if is_price else "price_column_empty",
            "current_price_verified": False,
            "training_authorized": False,
            "canonical_importable": False,
        })
    return output


def main() -> None:
    result = audit_research_ru_prices()
    print(json.dumps({
        "sources_inspected": len(result),
        "new_rub_price_files": sum(row["native_rub_verified"] for row in result),
        "new_product_row_candidates": sum(row["candidate_product_rows"] for row in result),
        "training_approved": False,
        "current_price_verified": False,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
