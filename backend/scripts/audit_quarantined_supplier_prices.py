"""Offline audit of quarantined public Russian supplier price-list originals.

This audit verifies provenance, explicit native-RUB evidence and structural rows.
It never authorizes current-price quoting, redistribution, canonical publishing or ML.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zipfile import BadZipFile, ZipFile

import xlrd
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[2] / "ml/data/real/research_unverified_supplier_prices"
MAX_MANIFEST_BYTES = 100_000
MAX_SOURCE_BYTES = 8_000_000
MAX_XLSX_ENTRIES = 8_000
MAX_XLSX_EXPANDED_BYTES = 160_000_000


@dataclass(frozen=True, slots=True)
class ExpectedSource:
    source_id: str
    source_page: str
    source_file_url: str
    filename: str
    sha256: str
    byte_count: int
    audit_profile: str


EXPECTED_SOURCES: dict[str, ExpectedSource] = {
    "bestmebel_furniture": ExpectedSource(
        "bestmebel_furniture",
        "https://www.yar.bestmebelshop.ru/mebel-optom/",
        "https://www.yar.bestmebelshop.ru/upload/opt/price.xlsx",
        "bestmebel_furniture.xlsx",
        "6522b41fd45e5c31c6a3d769951178a70540c2964cf9d40451d9604007647128",
        7_023_103,
        "quarantined_unverified",
    ),
    "krepiz_fasteners_2026": ExpectedSource(
        "krepiz_fasteners_2026",
        "https://www.krepiz.ru/price/",
        "https://www.krepiz.ru/price/price_Krepiz_28.09.26.xlsx",
        "krepiz_fasteners_2026.xlsx",
        "d58c23474a96b3a91c625f97ad6137500bf997dff4f8bf47547c38cd7c785138",
        1_909_957,
        "krepiz_xlsx",
    ),
    "donballon_new_products": ExpectedSource(
        "donballon_new_products",
        "https://www.donballon.ru/buyers/b2b/",
        "https://www.donballon.ru/upload/news.xlsx?v=1791548133",
        "donballon_new_products.xlsx",
        "49b3c9ad7b51f43db10876561daed12d1c231a39da73676928a1bca1bb36581b",
        288_879,
        "quarantined_unverified",
    ),
    "virsan_sanitary_20260915": ExpectedSource(
        "virsan_sanitary_20260915",
        "https://virsan.ru/price_list/",
        "https://virsan.ru/index.php?download_id=10&route=information%2Fprice_list%2Fdownload",
        "virsan_sanitary_20260915.xls",
        "eb69370ff9e9e2582f25b1fbef5a906b8d7103c84bc164b725f373dcca403233",
        6_434_304,
        "virsan_xls",
    ),
}


def _positive_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return False
    amount = Decimal(str(value))
    return amount.is_finite() and amount > 0


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_xlsx_archive(path: Path) -> None:
    try:
        with ZipFile(path) as archive:
            entries = archive.infolist()
            names = {item.filename for item in entries}
            if not {"[Content_Types].xml", "xl/workbook.xml"} <= names:
                raise ValueError("Krepiz source is not a valid XLSX archive.")
            if (
                len(entries) > MAX_XLSX_ENTRIES
                or sum(item.file_size for item in entries) > MAX_XLSX_EXPANDED_BYTES
            ):
                raise ValueError("Krepiz XLSX archive exceeds safe parsing limits.")
            if any(
                chr(92) in item.filename
                or ".." in item.filename.split("/")
                or item.flag_bits & 1
                for item in entries
            ):
                raise ValueError("Unsafe or encrypted Krepiz XLSX archive.")
    except BadZipFile as error:
        raise ValueError("Krepiz supplier original is not a valid XLSX archive.") from error


def _audit_krepiz_file(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("Krepiz source file missing or outside the bounded file policy.")
    _validate_xlsx_archive(path)
    book = load_workbook(path, read_only=True, data_only=True)
    try:
        if len(book.worksheets) != 1:
            raise ValueError("Krepiz worksheet count changed.")
        sheet = book.worksheets[0]
        if (sheet.title, sheet.max_row, sheet.max_column) != ("TDSheet", 28_543, 10):
            raise ValueError("Krepiz worksheet dimensions changed.")
        if sheet["A1"].value != "Прайс от 28.09.2026":
            raise ValueError("Krepiz source date changed.")
        note = sheet["C2"].value
        if not isinstance(note, str) or "Цена с НДС за ед.изм. при заказе на сумму" not in note:
            raise ValueError("Krepiz original price/VAT statement is missing.")
        expected_headers = (
            "Наименование", "Ед.изм", "Спец. Цена",
            "от 120 до 200 т.руб.", "От 60 до 120 т.руб.",
            "От 30 до 60 т.руб.", "От 8 до 30 т.руб.",
            "От 3 до 8 т.руб.", "Вес ед. измерения (кг)", "Артикул",
        )
        headers = tuple(sheet.cell(row=3, column=column).value for column in range(1, 11))
        if headers != expected_headers:
            raise ValueError("Krepiz source headers or price-tier meaning changed.")
        if not all("т.руб." in str(value) for value in headers[3:8]):
            raise ValueError("Krepiz order-value tiers no longer explicitly denote RUB.")

        identity_rows = 0
        complete_price_rows = 0
        incomplete_price_rows = 0
        missing_unit_rows = 0
        sku_rows: dict[str, list[int]] = defaultdict(list)
        units: Counter[str] = Counter()
        for row_number, row in enumerate(
            sheet.iter_rows(min_row=4, max_col=10, values_only=True), start=4,
        ):
            name, unit, *tail = row
            prices = tail[:6]
            sku = tail[7]
            if not (_nonempty(name) and _nonempty(sku)):
                continue
            identity_rows += 1
            sku_rows[str(sku).strip()].append(row_number)
            if _nonempty(unit):
                units[str(unit).strip()] += 1
            else:
                missing_unit_rows += 1
            if len(prices) == 6 and all(_positive_number(value) for value in prices):
                complete_price_rows += 1
            else:
                incomplete_price_rows += 1

        duplicate_groups = {
            sku: rows for sku, rows in sku_rows.items() if len(rows) > 1
        }
        if (
            identity_rows != 26_916
            or complete_price_rows != 26_916
            or incomplete_price_rows != 0
            or missing_unit_rows != 0
            or len(duplicate_groups) != 1
        ):
            raise ValueError("Krepiz product/price-row structure differs from the audited original.")
        return {
            "source_id": "krepiz_fasteners_2026",
            "source_host": "www.krepiz.ru",
            "source_document_date": "2026-09-28",
            "original_format": "XLSX",
            "native_rub_verified": True,
            "currency_evidence": (
                "Workbook states prices include VAT; five order thresholds explicitly use т.руб.; "
                "all six per-unit price tiers are positive numeric values."
            ),
            "product_identity_rows": identity_rows,
            "candidate_price_rows_verified": complete_price_rows,
            "incomplete_price_rows": incomplete_price_rows,
            "price_tier_count": 6,
            "unique_sku_count": len(sku_rows),
            "duplicate_sku_groups": len(duplicate_groups),
            "missing_unit_rows": missing_unit_rows,
            "unit_counts": dict(sorted(units.items())),
            "integrity_verified": True,
            "current_price_verified": False,
            "rights_reviewed": False,
            "canonical_importable": False,
            "training_authorized": False,
            "status": "quarantined_research_only",
        }
    finally:
        book.close()


def _audit_virsan_file(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("Virsan source file missing or outside the bounded file policy.")
    if path.read_bytes()[:8] != bytes.fromhex("D0CF11E0A1B11AE1"):
        raise ValueError("Virsan original is not a legacy Excel XLS file.")
    book = xlrd.open_workbook(str(path), on_demand=True)
    try:
        if book.nsheets != 1:
            raise ValueError("Virsan worksheet count changed.")
        sheet = book.sheet_by_index(0)
        if (sheet.name, sheet.nrows, sheet.ncols) != ("Sheet1", 6_411, 12):
            raise ValueError("Virsan worksheet dimensions changed.")
        date_value = sheet.cell_value(2, 2)
        currency_value = sheet.cell_value(4, 2)
        if " ".join(str(date_value).split()) != "Данные на: 15.09.2026":
            raise ValueError("Virsan source date changed.")
        if currency_value != "Цены товара в рублях.":
            raise ValueError("Virsan original native-RUB statement is missing.")
        if (
            sheet.cell_value(16, 2) != "Товар"
            or sheet.cell_value(16, 5) != "Код"
            or sheet.cell_value(16, 6) != "Виды цен (рубли)"
            or [
                sheet.cell_value(17, column) for column in (6, 7, 8)
            ] != ["Оптовая", "Крупный опт", "Специальная"]
            or sheet.cell_value(16, 9) != "Ед."
            or sheet.cell_value(16, 10) != "Упаковка"
            or sheet.cell_value(16, 11) != "Наличие"
        ):
            raise ValueError("Virsan price columns or tier semantics changed.")

        identity_rows = 0
        complete_price_rows = 0
        incomplete_price_rows = 0
        missing_unit_rows = 0
        sku_rows: dict[str, list[int]] = defaultdict(list)
        units: Counter[str] = Counter()
        for row_index in range(20, sheet.nrows):
            name = sheet.cell_value(row_index, 2)
            sku = sheet.cell_value(row_index, 5)
            prices = [sheet.cell_value(row_index, column) for column in (6, 7, 8)]
            unit = sheet.cell_value(row_index, 9)
            if not (_nonempty(name) and _nonempty(sku)):
                continue
            identity_rows += 1
            sku_rows[str(sku).strip()].append(row_index + 1)
            if _nonempty(unit):
                units[str(unit).strip()] += 1
            else:
                missing_unit_rows += 1
            if all(_positive_number(value) for value in prices):
                complete_price_rows += 1
            else:
                incomplete_price_rows += 1

        duplicate_groups = {
            sku: rows for sku, rows in sku_rows.items() if len(rows) > 1
        }
        if (
            identity_rows != 6_035
            or complete_price_rows != 6_031
            or incomplete_price_rows != 4
            or missing_unit_rows != 0
            or duplicate_groups
        ):
            raise ValueError("Virsan product/price-row structure differs from the audited original.")
        return {
            "source_id": "virsan_sanitary_20260915",
            "source_host": "virsan.ru",
            "source_document_date": "2026-09-15",
            "original_format": "XLS",
            "native_rub_verified": True,
            "currency_evidence": (
                "Workbook explicitly says Цены товара в рублях and Виды цен (рубли); "
                "three named price tiers are present."
            ),
            "product_identity_rows": identity_rows,
            "candidate_price_rows_verified": complete_price_rows,
            "incomplete_price_rows": incomplete_price_rows,
            "price_tier_count": 3,
            "unique_sku_count": len(sku_rows),
            "duplicate_sku_groups": len(duplicate_groups),
            "missing_unit_rows": missing_unit_rows,
            "unit_counts": dict(sorted(units.items())),
            "integrity_verified": True,
            "current_price_verified": False,
            "rights_reviewed": False,
            "canonical_importable": False,
            "training_authorized": False,
            "status": "quarantined_research_only",
        }
    finally:
        book.release_resources()


def _assert_manifest_audit_fields(
    record: dict[str, Any],
    result: dict[str, object],
) -> None:
    if record.get("native_rub_verified") is not True:
        raise ValueError("Manifest does not record the audited native-RUB evidence.")
    if record.get("source_sku_structure_verified") is not True:
        raise ValueError("Manifest does not record the audited source-SKU structure.")
    if record.get("source_sku_verified") is not False:
        raise ValueError("Unreviewed source SKUs must not be called human-verified.")
    field_pairs = (
        ("product_identity_rows_verified", "product_identity_rows"),
        ("price_rows_verified", "candidate_price_rows_verified"),
        ("incomplete_price_rows", "incomplete_price_rows"),
        ("price_tier_count", "price_tier_count"),
        ("duplicate_sku_groups", "duplicate_sku_groups"),
    )
    for manifest_field, report_field in field_pairs:
        if record.get(manifest_field) != result.get(report_field):
            raise ValueError(f"Supplier audit metadata mismatch: {manifest_field}.")


def audit_quarantined_supplier_prices(
    root: Path = ROOT,
) -> dict[str, object]:
    root = root.resolve()
    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("Quarantined supplier manifest is missing or redirected.")
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("Quarantined supplier manifest exceeds safe limits.")
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("schema_version") != "unverified-russian-supplier-format-research-v2"
        or manifest.get("training_approved") is not False
        or not isinstance(manifest.get("other_sources"), list)
    ):
        raise ValueError("Quarantined supplier manifest has an unexpected schema or approval flag.")
    primary = manifest.get("source")
    other_sources = manifest["other_sources"]
    if not isinstance(primary, dict) or any(not isinstance(item, dict) for item in other_sources):
        raise ValueError("Malformed supplier source records.")
    records: list[dict[str, Any]] = [primary, *other_sources]
    if {record.get("source_id") for record in records} != set(EXPECTED_SOURCES):
        raise ValueError("Supplier source IDs differ from the pinned research manifest.")

    for record in records:
        source_id = record.get("source_id")
        if not isinstance(source_id, str) or source_id not in EXPECTED_SOURCES:
            raise ValueError("Unrecognized supplier source ID.")
        expected = EXPECTED_SOURCES[source_id]
        for field, value in (
            ("source_page", expected.source_page),
            ("source_file_url", expected.source_file_url),
            ("file", expected.filename),
            ("sha256", expected.sha256),
            ("bytes", expected.byte_count),
        ):
            if record.get(field) != value:
                raise ValueError(f"Supplier source metadata changed: {source_id}.{field}")
        if (
            record.get("model_training_authorized") is not False
            or record.get("canonical_importable") is not False
            or record.get("current_price_verified") is not False
        ):
            raise ValueError("Unreviewed source cannot be promoted to training/import/current-price data.")
        rights_flag = record.get("rights_reviewed", record.get("reuse_rights_reviewed"))
        if rights_flag is not False:
            raise ValueError("Supplier reuse rights must remain unapproved.")
        page_host = urlparse(expected.source_page).hostname
        file_host = urlparse(expected.source_file_url).hostname
        if not page_host or page_host != file_host:
            raise ValueError("Supplier page and source file must share the pinned official host.")
        source_path = root / expected.filename
        if (
            source_path.is_symlink()
            or not source_path.is_file()
            or source_path.resolve().parent != root
        ):
            raise ValueError(f"Supplier source file is missing or redirected: {source_id}")
        if source_path.stat().st_size != expected.byte_count:
            raise ValueError(f"Supplier source byte count mismatch: {source_id}")
        content = source_path.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected.sha256:
            raise ValueError(f"Supplier source checksum mismatch: {source_id}")

    record_by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        source_id = record.get("source_id")
        if not isinstance(source_id, str):
            raise TypeError("Supplier source ID must be a string.")
        record_by_id[source_id] = record

    audited_sources: list[dict[str, object]] = []
    for source_id, expected in EXPECTED_SOURCES.items():
        if expected.audit_profile == "krepiz_xlsx":
            result = _audit_krepiz_file(root / expected.filename)
            _assert_manifest_audit_fields(record_by_id[source_id], result)
            audited_sources.append(result)
        elif expected.audit_profile == "virsan_xls":
            result = _audit_virsan_file(root / expected.filename)
            _assert_manifest_audit_fields(record_by_id[source_id], result)
            audited_sources.append(result)
        else:
            if record_by_id[source_id].get("native_rub_verified", False) is not False:
                raise ValueError("Unaudited supplier source cannot claim verified native RUB.")
            audited_sources.append({
                "source_id": source_id,
                "source_host": urlparse(expected.source_page).hostname or "",
                "original_format": Path(expected.filename).suffix.lstrip(".").upper(),
                "integrity_verified": True,
                "price_layout_audited": False,
                "candidate_price_rows_verified": 0,
                "native_rub_verified": False,
                "current_price_verified": False,
                "rights_reviewed": False,
                "canonical_importable": False,
                "training_authorized": False,
                "status": "quarantined_unverified",
            })

    audited_priced_sources = [
        item for item in audited_sources if item.get("native_rub_verified") is True
    ]
    candidate_rows_total = 0
    for item in audited_priced_sources:
        candidate_rows = item.get("candidate_price_rows_verified", 0)
        if not isinstance(candidate_rows, int):
            raise TypeError("Audited candidate-row count must be an integer.")
        candidate_rows_total += candidate_rows
    return {
        "schema_version": "quarantined-russian-price-audit-v1",
        "source_count": len(audited_sources),
        "structurally_audited_native_rub_sources": len(audited_priced_sources),
        "candidate_price_rows_verified": candidate_rows_total,
        "current_prices_verified": False,
        "reuse_rights_reviewed": False,
        "canonical_importable": False,
        "training_authorized": False,
        "sources": audited_sources,
    }


def main() -> None:
    print(json.dumps(audit_quarantined_supplier_prices(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
