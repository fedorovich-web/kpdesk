"""Offline audit of genuine, locally stored supplier price files.

Product-row counts are candidates by supplier-specific structural rules, not
independently validated catalog entries or ML gold labels. No network or DB.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

from openpyxl import load_workbook

from kpdesk.catalog.russian_store_policy import inspect_russian_store_price

ROOT = Path(__file__).resolve().parents[2] / "ml/data/real/public_supplier_prices"


def _positive_price(value: object) -> bool:
    return (
        isinstance(value, (int, float, Decimal))
        and not isinstance(value, bool)
        and value > 0
    )


def _count_product_candidates(path: Path, supplier_id: str) -> tuple[int, bool]:
    book = load_workbook(path, read_only=True, data_only=True)
    count = 0
    banner_2023 = False
    try:
        sheet = book.worksheets[0]
        if sheet.max_row > 100_000:
            raise ValueError("Unbounded source worksheet rejected")
        for i, row in enumerate(sheet.iter_rows(max_col=8, values_only=True)):
            if i < 4 and any("2023" in str(value) for value in row if value is not None):
                banner_2023 = True
            if supplier_id == "svih_lighting":
                if len(row) < 5:
                    continue
                sku, title, price = row[2], row[3], row[4]
                is_candidate = (
                    isinstance(sku, str) and bool(sku.strip())
                    and isinstance(title, str) and bool(title.strip())
                    and _positive_price(price)
                )
            else:
                if len(row) < 6:
                    continue
                title, link, price = row[2], row[3], row[5]
                is_candidate = (
                    isinstance(title, str) and bool(title.strip())
                    and isinstance(link, str)
                    and urlparse(link).hostname == "stroymaterialno.ru"
                    and _positive_price(price)
                )
            count += int(is_candidate)
    finally:
        book.close()
    return count, banner_2023


def audit_real_supplier_corpus(path: Path = ROOT) -> list[dict[str, str | int | bool]]:
    manifest_file = path / "manifest.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if manifest.get("dataset_type") != "unlabeled_real_supplier_price_format_research":
        raise ValueError("Unexpected corpus type; cannot verify provenance")
    verified = []
    for source in manifest["sources"]:
        if source.get("provenance") != "real_public_supplier_file":
            raise ValueError("Corpus contains failed or unsupported collection")
        if (
            source.get("model_training_authorized") is not False
            or source.get("labels") != "not_human_reviewed"
            or source.get("canonical_importable") is not False
            or source.get("prices_verified_current") is not False
        ):
            raise ValueError("Unreviewed supplier rows cannot be promoted to training gold")
        source_id = source["source_id"]
        host = urlparse(source["source_file_url"]).hostname
        if source_id not in {
            "svih_lighting", "stroymaterialno_ventilation",
            "stroymaterialno_consumables", "stroymaterialno_plumbing",
            "stroymaterialno_construction", "stroymaterialno_electrics",
        } or host not in {"www.svihcompany.ru", "stroymaterialno.ru"}:
            raise ValueError("Unrecognized or unapproved source")
        filename = source["file"]
        if filename != source_id + ".xlsx":
            raise ValueError("Source filename does not match the source ID")
        original = path / filename
        content = original.read_bytes()
        if hashlib.sha256(content).hexdigest() != source["sha256"]:
            raise ValueError("Source checksum mismatch")
        if len(content) != source["bytes"]:
            raise ValueError("Source file size mismatch")
        ru_price = inspect_russian_store_price(original, source)
        candidates, banner_2023 = _count_product_candidates(original, source_id)
        if banner_2023 != source["source_document_date"].startswith("2023"):
            raise ValueError("Supplier date evidence inconsistent with document")
        verified.append({
            "source_id": source_id,
            "source_host": host,
            "candidate_product_rows": candidates,
            "integrity_verified": True,
            "ready_for_supervised_training": False,
            "source_year_2023": banner_2023,
            "seller_country": ru_price["seller_country"],
            "price_currency": ru_price["price_currency"],
            "currency_evidence": ru_price["currency_evidence"],
            "eligible_for_current_price_quotes": False,
        })
    if len({item["source_id"] for item in verified}) != len(verified):
        raise ValueError("Duplicate real supplier sources")
    return verified


def main() -> None:
    summary = audit_real_supplier_corpus()
    print(json.dumps(summary, ensure_ascii=True, indent=2))
    print("TOTAL_CANDIDATE_ROWS", sum(int(x["candidate_product_rows"]) for x in summary))
    print("CORPUS_STATUS unlabeled; not approved for model training")


if __name__ == "__main__":
    main()
