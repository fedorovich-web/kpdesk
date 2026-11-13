"""Bounded acquisition of freely published supplier price files for local format research.

This is not a crawler, model-training dataset, ground truth, licensed resale
feed, or import endpoint. One explicit fetch per vendor-published URL.
The files stay on the Home-PC and never enter a live KPDesk catalog.
"""

from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zipfile import BadZipFile, ZipFile

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[2]
DEST = ROOT / "ml" / "data" / "real" / "public_supplier_prices"
MAX_FILE_BYTES = 8_000_000
MAX_DECOMPRESSED_BYTES = 160_000_000
MAX_ENTRIES = 8000
UA = "KPDesk/0.1 (offline catalog format research; contact support@kpdesk.ru)"
SOURCES = (
    (
        "svih_lighting",
        "https://www.svihcompany.ru/prices/",
        "https://www.svihcompany.ru/uploads/price-svih.xlsx",
    ),
    (
        "stroymaterialno_ventilation",
        "https://stroymaterialno.ru/prices/",
        "https://stroymaterialno.ru/prices/download/169/",
    ),
    (
        "stroymaterialno_consumables",
        "https://stroymaterialno.ru/prices/",
        "https://stroymaterialno.ru/prices/download/173/",
    ),
    (
        "stroymaterialno_plumbing",
        "https://stroymaterialno.ru/prices/",
        "https://stroymaterialno.ru/prices/download/226/",
    ),
    (
        "stroymaterialno_construction",
        "https://stroymaterialno.ru/prices/",
        "https://stroymaterialno.ru/prices/download/247/", 
    ),
    (
        "stroymaterialno_electrics",
        "https://stroymaterialno.ru/prices/",
        "https://stroymaterialno.ru/prices/download/295/", 
    ),
)


def _open_public_xlsx(source_url: str) -> tuple[bytes, str]:
    domain = urlparse(source_url).hostname
    if domain not in {"www.svihcompany.ru", "stroymaterialno.ru"}:
        raise ValueError("Unapproved supplier host")
    with urlopen(Request(source_url, headers={"User-Agent": UA}), timeout=12) as resp:
        final_url = resp.geturl()
        if urlparse(final_url).hostname != domain or urlparse(final_url).scheme != "https":
            raise ValueError("Redirected off approved HTTPS supplier host")
        content = resp.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise ValueError("Price exceeds per-file download limit")
    if len(content) < 100 or content[:2] != b"PK":
        raise ValueError("Received HTML or another unsupported document, not XLSX")
    with ZipFile(io.BytesIO(content)) as book:
        details = book.infolist()
        if len(details) > MAX_ENTRIES:
            raise ValueError("Excessive XLSX entry count")
        if sum(item.file_size for item in details) > MAX_DECOMPRESSED_BYTES:
            raise ValueError("Excessive XLSX decompressed size")
        if not {"[Content_Types].xml", "xl/workbook.xml"} <= set(book.namelist()):
            raise ValueError("ZIP is not a valid XLSX workbook")
        if any(item.filename.startswith(("/", "\\")) or ".." in item.filename.split("/")
               for item in details):
            raise ValueError("Unsafe ZIP paths")
    return content, final_url


def _structure(content: bytes) -> dict[str, list[dict[str, str | int | None]]]:
    workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=False)
    try:
        sheets: list[dict[str, str | int | None]] = []
        for sheet in workbook.worksheets[:8]:
            sheets.append({
                "sheet": sheet.title[:80],
                "reported_rows": sheet.max_row,
                "reported_columns": sheet.max_column,
            })
        return {"sheets": sheets}
    finally:
        workbook.close()


def main() -> None:
    DEST.mkdir(parents=True, exist_ok=True)
    manifest_path = DEST / "manifest.json"
    if manifest_path.exists():
        raise RuntimeError("Existing manifest must be reviewed; refusing silent replacement.")
    entries = []
    for source_id, source_page, url in SOURCES:
        try:
            content, actual = _open_public_xlsx(url)
            filename = source_id + ".xlsx"
            target = DEST / filename
            if target.exists():
                raise RuntimeError("Existing supplier file must not be overwritten.")
            schema = _structure(content)
            sha256 = hashlib.sha256(content).hexdigest()
            target.write_bytes(content)
            record = {
                "source_id": source_id,
                "source_page": source_page,
                "source_file_url": actual,
                "retrieved_utc": datetime.now(UTC).isoformat(),
                "sha256": sha256,
                "file": filename,
                "bytes": len(content),
                "provenance": "real_public_supplier_file",
                "labels": "not_human_reviewed",
                "model_training_authorized": False,
                "canonical_importable": False,
                "licensing_or_terms": "requires_independent_review_before_training_or_redistribution",
                **schema,
            }
            entries.append(record)
            print("DOWNLOADED", source_id, "bytes",len(content),
                  "sheets",len(schema["sheets"]), "sha256",sha256[:12])
        except (OSError, BadZipFile, ValueError) as error:
            entries.append({
                "source_id": source_id,
                "source_page": source_page,
                "source_file_url": url,
                "error": type(error).__name__ + ": " + str(error)[:100],
                "provenance": "download_failed",
            })
            print("SKIPPED",source_id, type(error).__name__)
    manifest_path.write_text(json.dumps({
        "dataset_type": "unlabeled_real_supplier_price_format_research",
        "schema_version": 1,
        "never_train_without_rights_and_labels": True,
        "sources": entries,
    }, ensure_ascii=False, indent=2),encoding="utf-8")
    print("MANIFEST",manifest_path, "downloaded",sum("sha256" in x for x in entries))


if __name__=="__main__":
    main()
