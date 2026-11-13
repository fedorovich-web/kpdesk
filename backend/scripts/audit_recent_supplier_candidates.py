"""Offline integrity and structural quarantine gate for 2026-10-21 supplier originals.

Never promotes these sources to live prices, human labels or training data.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
from decimal import Decimal
from pathlib import Path

import xlrd
from openpyxl import load_workbook
from xlrd.compdoc import CompDocError

from scripts.audit_supplier_additions import audit_shveigid, audit_technoas
from scripts.supplier_xlsx_validation import MAX_FILE_BYTES, validate_xlsx_archive

ROOT = Path(__file__).resolve().parents[2] / "ml/data/real/research_20261021_supplier_prices"
# Immutable evidence independently pinned from the research manifest.
EXPECTED = {
    "viz_tools_20261019": (
        "https://vizinstrument.ru/pricelist.html",
        "https://vizinstrument.ru/data/price-list.xlsx",
        "viz_tools_20261019.xlsx",
        "e55055dff8cd44257eb0823d84e35493db6d16ef2025c06ef8786441f16a5178",
        4_781_497,
        9_712,
        "quarantined_price_currency_and_semantics_unverified",
    ),
    "dselektric_20261021": (
        "https://dselektric.ru/price-list",
        "https://dselektric.ru/dselektric-price.xls",
        "dselektric_20261021.xls",
        "519a85bf1bca70e09ab502d8c0fa912991421a74226875672c47fb38b39b925d",
        866_816,
        0,
        "quarantined_strict_xls_parse_failed",
    ),
    "avt_el_ecoplast_20260615": (
        "https://avt-el.ru/price/",
        "https://avt-el.ru/wa-data/public/site/price-ecoplast-2026-06-15.xlsx",
        "avt_el_ecoplast_20260615.xlsx",
        "ead53828a1c21ed0aaa2111a1fa33f4b482a8fb138ec30daa9ea126b8215eb03",
        447_977,
        3_595,
        "quarantined_structural_rub_prices_only",
    ),
    "etk_rezerv_20260316": (
        "https://etk-rezerv.ru/praise.html",
        "https://etk-rezerv.ru/16032026.xlsx",
        "etk_rezerv_20260316.xlsx",
        "6df6e16d199851ac7fb962a806c67822c1bd81ad905ba6d8ad07ea6d660e525c",
        203_474,
        252,
        "quarantined_multisheet_literal_rub_only",
    ),
    "shveigid_20261021": (
        "https://shveigid.ru/info/price/",
        "https://shveigid.ru/info/price/download/",
        "shveigid_20261021.xlsx",
        "ce92993f17c8e31bfdb52cbfd6285f876ee4e10d529886d87121192305c23e83",
        2_384_181,
        17_922,
        "quarantined_native_rub_multicurrency_price_research",
    ),
    "technoas_20260916": (
        "https://www.technoac.ru/company/news/shirokiy-vybor-oborudovaniya-vedushchikh-proizvoditeley/",
        "https://www.technoac.ru/files/Techno-As-Price-16_09_26.xlsx",
        "technoas_20260916.xlsx",
        "2d71f5d838a16479e897d2c9bcce650cb33c84bd37fadff47bffd7e7a1bdf735",
        61_908,
        354,
        "quarantined_price_currency_unverified_vat_layout",
    ),
}
BLOCKED_APPROVAL_FLAGS = (
    "price_as_of_verified",
    "source_sku_semantics_verified",
    "unit_package_verified",
    "current_price_verified",
    "reuse_rights_reviewed",
    "training_authorized",
    "canonical_importable",
)


def _audit_viz(path: Path) -> int:
    validate_xlsx_archive(path)
    book = load_workbook(path, read_only=True, data_only=True, keep_links=False)
    try:
        if len(book.worksheets) != 1:
            raise ValueError("VIZ worksheet count changed.")
        sheet = book.worksheets[0]
        if (sheet.max_row, sheet.max_column) != (11_500, 43):
            raise ValueError("VIZ worksheet dimensions changed.")
        if (
            sheet["A6"].value != "АРТ"
            or sheet["B6"].value != "НАИМЕНОВАНИЯ"
            or sheet["F6"].value != "Цена\nС НДС"
        ):
            raise ValueError("VIZ product/price header layout changed.")
        count = 0
        for row in sheet.iter_rows(min_row=7, max_col=6, values_only=True):
            name, price = row[1], row[5]
            if not isinstance(name, str) or not name.strip():
                continue
            if isinstance(price, bool) or not isinstance(price, (int, float)):
                continue
            value = Decimal(str(price))
            if value.is_finite() and value > 0:
                count += 1
        return count
    finally:
        book.close()


def _audit_ecoplast(path: Path) -> dict[str, int]:
    """Count source-denominated RUB price rows without assuming offer validity."""
    validate_xlsx_archive(path)
    book = load_workbook(path, read_only=True, data_only=True, keep_links=False)
    try:
        if len(book.worksheets) != 1:
            raise ValueError("Ecoplast worksheet count changed.")
        sheet = book.worksheets[0]
        if (sheet.title, sheet.max_row, sheet.max_column) != ("Прайс-лист", 4_687, 13):
            raise ValueError("Ecoplast worksheet shape changed.")
        if sheet["B1"].value != "Прайс лист от 15 июня 2026 г.":
            raise ValueError("Ecoplast source document date changed.")
        headings = tuple(sheet.cell(2, column).value for column in range(1, 7))
        if headings != (
            "Артикул", "Наименование", "Ед. изм.",
            "Количество\nв упаковке", "Цена", "Валюта",
        ):
            raise ValueError("Ecoplast SKU/unit/pack/price/currency headings changed.")

        identity_rows = native_rub_labeled_rows = complete_rows = 0
        missing_price_rows = invalid_currency_rows = invalid_package_rows = 0
        sku_rows: dict[str, int] = {}
        for sku, name, unit, package, price, currency in sheet.iter_rows(
            min_row=3, max_col=6, values_only=True,
        ):
            if (
                isinstance(sku, bool)
                or not isinstance(sku, (str, int))
                or not str(sku).strip()
                or not isinstance(name, str)
                or not name.strip()
            ):
                continue
            identity_rows += 1
            sku_key = str(sku).strip()
            sku_rows[sku_key] = sku_rows.get(sku_key, 0) + 1

            native_rub = isinstance(currency, str) and currency.strip() == "руб."
            positive_price = (
                not isinstance(price, bool)
                and isinstance(price, (int, float))
                and Decimal(str(price)).is_finite()
                and Decimal(str(price)) > 0
            )
            valid_unit = isinstance(unit, str) and unit.strip() in {
                "шт", "уп", "м", "бухта",
            }
            valid_package = (
                not isinstance(package, bool)
                and isinstance(package, (int, float))
                and package > 0
                and float(package).is_integer()
            )
            native_rub_labeled_rows += int(native_rub)
            missing_price_rows += int(not positive_price)
            invalid_currency_rows += int(not native_rub)
            invalid_package_rows += int(not valid_package)
            if native_rub and positive_price and valid_unit and valid_package:
                complete_rows += 1

        return {
            "identity_rows": identity_rows,
            "native_rub_labeled_rows": native_rub_labeled_rows,
            "candidate_native_rub_price_rows": complete_rows,
            "missing_or_invalid_price_rows": missing_price_rows,
            "invalid_currency_rows": invalid_currency_rows,
            "invalid_package_rows": invalid_package_rows,
            "duplicate_sku_groups": sum(count > 1 for count in sku_rows.values()),
        }
    finally:
        book.close()


def _audit_etk_rezerv(path: Path) -> dict[str, object]:
    """Separate literal RUB prices from untrusted cached formula results."""
    validate_xlsx_archive(path)
    values_book = load_workbook(path, read_only=True, data_only=True, keep_links=False)
    formulas_book = load_workbook(path, read_only=True, data_only=False, keep_links=False)
    try:
        if (
            len(values_book.worksheets) != 8
            or [sheet.title for sheet in values_book.worksheets]
            != [sheet.title for sheet in formulas_book.worksheets]
        ):
            raise ValueError("ETK worksheet identity or count changed.")

        years: list[int] = []
        literal_counts: list[int] = []
        formula_counts: list[int] = []
        for values_sheet, formulas_sheet in zip(
            values_book.worksheets, formulas_book.worksheets, strict=True,
        ):
            date_label = values_sheet["A8"].value
            if not isinstance(date_label, str):
                raise TypeError("ETK worksheet date missing.")
            found_date = re.search(r"\b\d{2}\.\d{2}\.(20\d{2})\b", date_label)
            if found_date is None:
                raise ValueError("ETK worksheet date invalid.")
            years.append(int(found_date.group(1)))
            if (
                values_sheet["A9"].value != "Наименование продукции"
                or values_sheet["H9"].value != "Цена, руб. с НДС"
            ):
                raise ValueError("ETK worksheet name/currency heading changed.")

            literal_positive = 0
            formula_cached_positive = 0
            for data_row, formula_row in zip(
                values_sheet.iter_rows(min_row=11, max_col=8, values_only=True),
                formulas_sheet.iter_rows(min_row=11, max_col=8),
                strict=True,
            ):
                name = data_row[0]
                price = data_row[7]
                if not isinstance(name, str) or not name.strip():
                    continue
                if (
                    isinstance(price, bool)
                    or not isinstance(price, (int, float))
                    or not Decimal(str(price)).is_finite()
                    or Decimal(str(price)) <= 0
                ):
                    continue
                if formula_row[7].data_type == "f":
                    formula_cached_positive += 1
                else:
                    literal_positive += 1
            literal_counts.append(literal_positive)
            formula_counts.append(formula_cached_positive)

        return {
            "candidate_native_rub_price_rows": sum(literal_counts),
            "formula_cached_positive_rows_unverified": sum(formula_counts),
            "source_sku_provided": False,
            "sheet_literal_counts": literal_counts,
            "sheet_formula_counts": formula_counts,
            "sheet_years": years,
        }
    finally:
        values_book.close()
        formulas_book.close()


def _verify_rejected_xls(path: Path) -> None:
    with path.open("rb") as stream:
        if stream.read(8) != bytes.fromhex("D0CF11E0A1B11AE1"):
            raise ValueError("DS Electric original is not OLE/XLS.")
    try:
        book = xlrd.open_workbook(str(path), on_demand=True, logfile=io.StringIO())
    except CompDocError:
        # The pinned original genuinely fails strict parsing; never repair silently.
        return
    book.release_resources()
    raise ValueError("DS Electric XLS parse state changed; human re-review is required.")


def audit_recent_supplier_candidates(root: Path = ROOT) -> dict[str, object]:
    root = root.resolve()
    manifest_path = root / "manifest.json"
    if (
        manifest_path.is_symlink()
        or not manifest_path.is_file()
        or manifest_path.stat().st_size > 16_384
    ):
        raise ValueError("Research manifest is missing, redirected or oversized.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != "research-supplier-quarantine-v1"
        or manifest.get("training_approved") is not False
        or not isinstance(manifest.get("sources"), list)
    ):
        raise ValueError("Unexpected research manifest schema or training approval.")
    records = manifest["sources"]
    if any(not isinstance(record, dict) for record in records):
        raise ValueError("Malformed source record.")
    if len(records) != len(EXPECTED) or {
        record.get("source_id") for record in records
    } != set(EXPECTED):
        raise ValueError("Missing, duplicate or unexpected source ID.")

    findings: list[dict[str, object]] = []
    unverified_currency_candidates = 0
    verified_rub_candidates = 0
    unverified_formula_candidates = 0
    for record in records:
        source_id = record["source_id"]
        page, url, filename, digest, size, expected_rows, status = EXPECTED[source_id]
        for field, expected in (
            ("source_page", page),
            ("source_file_url", url),
            ("file", filename),
            ("sha256", digest),
            ("bytes", size),
            ("status", status),
            ("structural_positive_price_candidates", expected_rows),
            ("native_rub_verified", source_id in (
                "avt_el_ecoplast_20260615", "etk_rezerv_20260316",
                "shveigid_20261021",
            )),
            ("verified_rub_price_rows", (
                expected_rows if source_id in (
                    "avt_el_ecoplast_20260615", "etk_rezerv_20260316",
                    "shveigid_20261021",
                ) else 0
            )),
        ):
            if record.get(field) != expected:
                raise ValueError(f"Research evidence changed: {source_id}.{field}")
        if any(record.get(field) is not False for field in BLOCKED_APPROVAL_FLAGS):
            raise ValueError("Quarantined source was promoted without human approval.")
        path = root / filename
        if (
            path.is_symlink()
            or not path.is_file()
            or path.resolve().parent != root
            or path.stat().st_size > MAX_FILE_BYTES
            or path.stat().st_size != size
        ):
            raise ValueError(f"Source file missing, redirected or resized: {source_id}")
        with path.open("rb") as stream:
            actual_digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual_digest != digest:
            raise ValueError(f"Source checksum changed: {source_id}")

        native_rub_rows = 0
        if source_id == "viz_tools_20261019":
            actual_rows = _audit_viz(path)
            if actual_rows != expected_rows:
                raise ValueError("VIZ structural candidate count changed.")
            unverified_currency_candidates += actual_rows
        elif source_id == "avt_el_ecoplast_20260615":
            details = _audit_ecoplast(path)
            for field, value in (
                ("source_document_date", "2026-06-15"),
                ("product_identity_rows", details["identity_rows"]),
                ("native_rub_labeled_rows", details["native_rub_labeled_rows"]),
                ("missing_or_invalid_price_rows", details["missing_or_invalid_price_rows"]),
                ("invalid_currency_rows", details["invalid_currency_rows"]),
                ("invalid_package_rows", details["invalid_package_rows"]),
                ("duplicate_sku_groups", details["duplicate_sku_groups"]),
            ):
                if record.get(field) != value:
                    raise ValueError(f"Ecoplast source audit metadata mismatch: {field}")
            actual_rows = details["candidate_native_rub_price_rows"]
            if actual_rows != expected_rows:
                raise ValueError("Ecoplast structural RUB candidate count changed.")
            native_rub_rows = actual_rows
            verified_rub_candidates += native_rub_rows
        elif source_id == "etk_rezerv_20260316":
            etk_details = _audit_etk_rezerv(path)
            for etk_field, etk_value in (
                ("source_document_years_by_sheet", etk_details["sheet_years"]),
                (
                    "formula_cached_positive_rows_unverified",
                    etk_details["formula_cached_positive_rows_unverified"],
                ),
            ):
                if record.get(etk_field) != etk_value:
                    raise ValueError(f"ETK source audit metadata mismatch: {etk_field}")
            if etk_details["source_sku_provided"] is not False:
                raise ValueError("ETK source SKU status changed.")
            etk_price_rows = etk_details["candidate_native_rub_price_rows"]
            if not isinstance(etk_price_rows, int):
                raise TypeError("ETK literal RUB price count must be an integer.")
            actual_rows = etk_price_rows
            if actual_rows != expected_rows:
                raise ValueError("ETK literal RUB candidate count changed.")
            native_rub_rows = expected_rows
            verified_rub_candidates += native_rub_rows
            cached_count = etk_details["formula_cached_positive_rows_unverified"]
            if not isinstance(cached_count, int):
                raise TypeError("ETK cached formula count must be an integer.")
            unverified_formula_candidates += cached_count
        elif source_id == "shveigid_20261021":
            source_report = audit_shveigid(path)
            for field in (
                "source_price_rows",
                "original_rub_currency_rows",
                "foreign_currency_rows_excluded",
                "missing_article_in_accepted_rows",
            ):
                if record.get(field) != source_report[field]:
                    raise ValueError(f"Shveigid source audit metadata mismatch: {field}")
            if record.get("source_claimed_generated_at") != (
                "2026-10-21 20:20 (timezone not independently validated)"
            ):
                raise ValueError("Shveigid source-generated timestamp changed.")
            actual_rows = source_report["native_rub_price_candidates"]
            if actual_rows != expected_rows:
                raise ValueError("Shveigid native RUB candidate count changed.")
            native_rub_rows = actual_rows
            verified_rub_candidates += actual_rows
        elif source_id == "technoas_20260916":
            source_report_vat = audit_technoas(path)
            if (
                record.get("source_document_date")
                != source_report_vat["source_document_date"]
                or record.get("source_worksheet_name") != "02.06.26"
                or record.get("calculated_non_vat_formula_rows")
                != source_report_vat["calculated_non_vat_formula_rows"]
                or source_report_vat["native_rub_price_candidates"] != 0
            ):
                raise ValueError("Techno-AS source audit metadata mismatch.")
            price_count = source_report_vat["literal_positive_vat_price_rows"]
            if not isinstance(price_count, int):
                raise TypeError("Techno-AS price count must be an integer.")
            actual_rows = price_count
            if actual_rows != expected_rows:
                raise ValueError("Techno-AS structure candidate count changed.")
            unverified_currency_candidates += actual_rows
        else:
            _verify_rejected_xls(path)
            actual_rows = 0
        findings.append({
            "source_id": source_id,
            "original_integrity_verified": True,
            "structural_positive_price_candidates": actual_rows,
            "native_rub_price_rows_verified": native_rub_rows,
            "canonical_importable": False,
            "training_authorized": False,
            "status": status,
        })
    return {
        "source_count": len(findings),
        "structural_positive_price_candidates_not_currency_verified": (
            unverified_currency_candidates
        ),
        "native_rub_price_rows_verified": verified_rub_candidates,
        "formula_cached_price_rows_unverified": unverified_formula_candidates,
        "training_authorized": False,
        "sources": findings,
    }


if __name__ == "__main__":
    print(json.dumps(audit_recent_supplier_candidates(), indent=2))
