"""Closed RU-store / RUB price provenance gate for genuine supplier originals.

This is a validation of the currently audited sources, not a generic way to
prove a site's legal registration from its TLD. New vendors require manual
verification and a new explicitly allowlisted source profile.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from openpyxl import load_workbook

_ALLOWED_SOURCES: dict[str, tuple[str, str]] = {
    "svih_lighting": ("www.svihcompany.ru", "www.svihcompany.ru"),
    "stroymaterialno_ventilation": ("stroymaterialno.ru", "stroymaterialno.ru"),
    "stroymaterialno_consumables": ("stroymaterialno.ru", "stroymaterialno.ru"),
    "stroymaterialno_plumbing": ("stroymaterialno.ru", "stroymaterialno.ru"),
    "stroymaterialno_construction": ("stroymaterialno.ru", "stroymaterialno.ru"),
    "stroymaterialno_electrics": ("stroymaterialno.ru", "stroymaterialno.ru"),
}


def _validate_site_url(url: object, expected_host: str) -> None:
    if not isinstance(url, str):
        raise TypeError("Russian store URL missing.")
    parts = urlparse(url)
    if (
        parts.scheme != "https" or parts.hostname != expected_host
        or parts.username or parts.password or parts.port is not None
    ):
        raise ValueError("Source is not a vetted Russian online store host.")


def _workbook_rub_evidence(path: Path, supplier_id: str) -> str:
    book = load_workbook(path, read_only=True, data_only=True)
    try:
        if not book.worksheets:
            raise ValueError("Supplier workbook has no worksheet.")
        sheet = book.worksheets[0]
        if sheet.max_row > 100_000 or sheet.max_column > 200:
            raise ValueError("Supplier workbook shape outside policy.")
        contents = [
            str(value).strip()
            for row in sheet.iter_rows(max_row=14, max_col=8, values_only=True)
            for value in row if isinstance(value, str)
        ]
        if supplier_id == "svih_lighting":
            if (
                not any("Цены в рублях" in item for item in contents)
                or not any("Цена с НДС, ₽" in item for item in contents)
            ):
                raise ValueError("RUB currency not verified in Russian source workbook.")
            return "workbook_currency_statement_rub"
        if not any(item == "Цена (руб.)" for item in contents):
            raise ValueError("RUB currency header missing in Russian store workbook.")
        return "workbook_header_rub"
    finally:
        book.close()


def inspect_russian_store_price(
    path: Path, record: dict[str, Any], *, verify_sha: bool = True,
) -> dict[str, str | int | bool]:
    """Return structural supplier currency evidence, not a fresh-price approval.

    Numeric price cells alone have no currency: the source's price-header
    statement must explicitly be denominated in rubles. Do not convert EUR/USD.
    """
    identity = record.get("source_id")
    if not isinstance(identity, str) or identity not in _ALLOWED_SOURCES:
        raise ValueError("Supplier not vetted as a Russian online store.")
    source_host, download_host = _ALLOWED_SOURCES[identity]
    _validate_site_url(record.get("source_page"), source_host)
    _validate_site_url(record.get("source_file_url"), download_host)
    if record.get("currency", "RUB") != "RUB":
        raise ValueError("Only source-denominated RUB currency prices are accepted.")
    if path.name != identity + ".xlsx" or path.is_symlink() or not path.is_file():
        raise ValueError("Unexpected Russian store original source file.")
    if not 1 <= path.stat().st_size <= 8_000_000:
        raise ValueError("Supplier source file size outside policy.")
    raw = path.read_bytes()
    if verify_sha and (
        hashlib.sha256(raw).hexdigest() != record.get("sha256")
        or len(raw) != record.get("bytes")
    ):
        raise ValueError("Russian source SHA256 or byte size mismatch.")

    evidence = _workbook_rub_evidence(path, identity)
    date = record.get("source_document_date")
    if not isinstance(date, str):
        raise TypeError("Supplier price document date unavailable.")
    if identity == "svih_lighting":
        if not date.startswith("2026-09-06"):
            raise ValueError("Unexpected published source document date.")
        year = 2026
    else:
        if not date.startswith("2023"):
            raise ValueError("Unexpected historical source document year.")
        year = 2023
    if record.get("prices_verified_current") is not False:
        raise ValueError("Supplier prices incorrectly asserted to be current.")
    return {
        "source_id": identity,
        "seller_country": "RU",
        "store_market": "russian_online_store",
        "price_currency": "RUB",
        "currency_evidence": evidence,
        "document_year": year,
        "price_verified_current": False,
        "eligible_for_current_price_quotes": False,
        "foreign_currency_conversion_permitted": False,
    }
