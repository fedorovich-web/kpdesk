"""Offline research checks for independently published sewing and instrument prices.

No source is a canonical import, a confirmed current offer, or ML training ground truth.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal
from pathlib import Path

from openpyxl import load_workbook

from scripts.supplier_xlsx_validation import validate_xlsx_archive


def _positive_price(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    price = Decimal(str(value))
    if not price.is_finite() or price <= 0:
        return None
    return price


def audit_shveigid(path: Path) -> dict[str, int]:
    """Count source-native RUB only; never count display-RUB for USD/EUR."""
    validate_xlsx_archive(path)
    book = load_workbook(path, read_only=True, data_only=False, keep_links=False)
    try:
        if [sheet.title for sheet in book.worksheets] != ["Прайс", "Сводка", "price"]:
            raise ValueError("Shveigid source worksheet identity changed.")
        display, summary, rows = book.worksheets
        if (
            (display.max_row, display.max_column) != (21_863, 8)
            or (rows.max_row, rows.max_column) != (21_859, 7)
            or (summary.max_row, summary.max_column) != (153, 5)
        ):
            raise ValueError("Shveigid XLSX worksheet shape changed.")
        if (
            not isinstance(display["A2"].value, str)
            or not display["A2"].value.startswith("Актуально на 21.10.2026 20:20")
            or not isinstance(summary["A2"].value, str)
            or not summary["A2"].value.startswith("Позиций в прайсе: 21858")
        ):
            raise ValueError("Shveigid self-reported snapshot count/time changed.")
        if tuple(display.cell(5, i).value for i in (4, 5, 6)) != (
            "Артикул", "Наименование", "Цена, ₽",
        ):
            raise ValueError("Shveigid display price/currency heading changed.")
        if tuple(rows.cell(1, i).value for i in range(1, 8)) != (
            "id", "article", "name", "price", "currency", "price_rub", "in_stock",
        ):
            raise ValueError("Shveigid raw currency and price columns changed.")

        source_rows = rub_rows = foreign_rows = accepted = missing_article = 0
        ids: Counter[str] = Counter()
        for display_row, raw in zip(
            display.iter_rows(min_row=6, max_col=8, values_only=True),
            rows.iter_rows(min_row=2, max_col=7, values_only=True),
            strict=True,
        ):
            source_rows += 1
            _, article, name, price, currency, rub, _ = raw
            if currency not in ("RUB", "USD", "EUR"):
                raise ValueError("Shveigid source currency is not recognized.")
            if currency != "RUB":
                foreign_rows += 1
                continue
            rub_rows += 1
            source_price = _positive_price(price)
            normalized_price = _positive_price(rub)
            if (
                not isinstance(name, str)
                or not name.strip()
                or source_price is None
                or normalized_price is None
            ):
                continue
            if (
                source_price != normalized_price
                or _positive_price(display_row[5]) != normalized_price
                or display_row[4] != name
            ):
                raise ValueError("Shveigid original RUB and rendered prices disagree.")
            row_id = raw[0]
            if isinstance(row_id, bool) or not isinstance(row_id, (int, str)):
                raise TypeError("Shveigid source row lacks a stable internal ID.")
            key = str(row_id).strip()
            if not key:
                raise ValueError("Shveigid source ID is blank.")
            ids[key] += 1
            missing_article += int(
                article is None or str(article).strip().lower() in ("", "none", "null")
            )
            accepted += 1
        if source_rows != 21_858:
            raise ValueError("Shveigid source row count changed.")
        if any(n > 1 for n in ids.values()):
            raise ValueError("Shveigid accepted source IDs are not unique.")
        return {
            "source_price_rows": source_rows,
            "original_rub_currency_rows": rub_rows,
            "foreign_currency_rows_excluded": foreign_rows,
            "native_rub_price_candidates": accepted,
            "missing_article_in_accepted_rows": missing_article,
            "duplicate_internal_id_groups": 0,
        }
    finally:
        book.close()


def audit_technoas(path: Path) -> dict[str, int | str]:
    """Count literal VAT-price layout, refusing to infer RUB from a VAT label."""
    validate_xlsx_archive(path)
    book = load_workbook(path, read_only=True, data_only=False, keep_links=False)
    try:
        if len(book.worksheets) != 1:
            raise ValueError("Techno-AS workbook contains unexpected sheets.")
        sheet = book.active
        if (
            sheet is None
            or sheet.title != "02.06.26"
            or (sheet.max_row, sheet.max_column) != (545, 28)
        ):
            raise ValueError("Techno-AS sheet identity or shape changed.")
        if (
            not isinstance(sheet["E8"].value, str)
            or "19.03.2026" not in sheet["E8"].value
        ):
            raise ValueError("Techno-AS document date changed.")
        if (
            sheet["C10"].value != "Код"
            or sheet["D10"].value != "Наименование продукции"
            or sheet["F10"].value != "Цена без НДС"
            or sheet["G10"].value != "Цена с НДС 22%"
        ):
            raise ValueError("Techno-AS VAT price heading changed.")
        priced_rows = no_vat_formula_rows = 0
        for row in sheet.iter_rows(min_row=11, max_col=7):
            _, _, code, name, _, without_vat, with_vat = row
            if (
                isinstance(code.value, bool)
                or not isinstance(code.value, (str, int))
                or not str(code.value).strip()
                or not isinstance(name.value, str)
                or not name.value.strip()
            ):
                continue
            if without_vat.data_type == "f":
                no_vat_formula_rows += 1
            if with_vat.data_type == "f":
                continue
            if _positive_price(with_vat.value) is not None:
                priced_rows += 1
        return {
            "source_document_date": "2026-03-19",
            "literal_positive_vat_price_rows": priced_rows,
            "calculated_non_vat_formula_rows": no_vat_formula_rows,
            "native_rub_price_candidates": 0,
        }
    finally:
        book.close()
