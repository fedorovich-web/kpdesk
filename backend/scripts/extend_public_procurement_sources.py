"""Acquire THREE additional public procurement originals; preserve prior dataset.

Create-only controlled HTTPS allowlist. The original two-file manifest is
preserved. This script writes manifest-v2.json with 5 original buyer docs.
All copies remain unapproved for training/reuse.
"""
from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zipfile import ZipFile

from scripts.collect_public_procurement_examples import OUT

SOURCES = (
    {
        "id": "bidzaar_284960_paper_2026",
        "source_page": "https://bidzaar.com/app/process/light/019bc56f-c495-776b-8bbd-c08fde222afb",
        "document_url": "https://bidzaar.com/api/filestorage/files/download/5959ae4f-70cd-4c82-b6de-61b440c41c58",
        "filename": "bidzaar_284960_paper_2026.docx",
        "advertised_document_date": "2026-01-16",
    },
    {
        "id": "bidzaar_337279_design_2026",
        "source_page": "https://bidzaar.com/app/process/light/019e877a-88f2-787e-b4d5-18a5f46ce8a2",
        "document_url": "https://bidzaar.com/api/filestorage/files/download/16be193d-1b6b-4228-96af-38b1f663d892",
        "filename": "bidzaar_337279_design_2026.xlsx",
        "advertised_document_date": "2026-06-02",
    },
    {
        "id": "bidzaar_364642_tmc_2026",
        "source_page": "https://bidzaar.com/app/process/light/01a01005-28e4-7989-96ca-6f1c01744dfe",
        "document_url": "https://bidzaar.com/api/filestorage/files/download/fe8ba7b2-f234-48e3-9a65-4da3eec7d98f",
        "filename": "bidzaar_364642_tmc_2026.xlsx",
        "advertised_document_date": "2026-08-17",
    },
)


def _read_first_manifest() -> list[dict[str, object]]:
    old = json.loads((OUT / "manifest.json").read_text(encoding="utf-8"))
    if old.get("schema_version") != "public-procurement-source-v1":
        raise ValueError("Original source manifest schema changed.")
    entries = old["entries"]
    if not isinstance(entries, list) or len(entries) != 2:
        raise ValueError("Expected exactly two original buyer documents.")
    for entry in entries:
        p = OUT / str(entry["filename"])
        if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError("Original buyer document no longer matches old manifest.")
    return entries


def main() -> None:
    old_entries = _read_first_manifest()
    destination = OUT / "manifest-v2.json"
    if destination.exists() or any((OUT / s["filename"]).exists() for s in SOURCES):
        raise FileExistsError("Preserve originals and manifest-v2; do not overwrite.")
    records: list[dict[str, object]] = []
    downloads: list[tuple[Path, bytes]] = []
    for entry in SOURCES:
        uri = entry["document_url"]
        if urlparse(uri).scheme != "https" or urlparse(uri).hostname != "bidzaar.com":
            raise ValueError("Document outside public allowlist.")
        with urlopen(Request(uri, headers={"User-Agent": "KPDesk source research"}), timeout=14) as resp:
            if urlparse(resp.geturl()).hostname != "bidzaar.com" or urlparse(resp.geturl()).scheme != "https":
                raise ValueError("Unexpected redirect.")
            raw = resp.read(2_000_001)
        if not 300 <= len(raw) <= 2_000_000 or not raw.startswith(b"PK"):
            raise ValueError("Original public document wrong size/format.")
        with ZipFile(io.BytesIO(raw)) as zipf:
            required = "word/document.xml" if entry["filename"].endswith(".docx") else "xl/workbook.xml"
            info = zipf.infolist()
            if (required not in zipf.namelist() or len(info) > 3000
                    or sum(item.file_size for item in info) > 50_000_000):
                raise ValueError("Unexpected OOXML archive contents.")
        meta: dict[str, object] = {
            **entry,
            "retrieved_at_utc": datetime.now(UTC).isoformat(),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "provenance_declared": "published_public_buyer_procurement_request",
            "origin_independently_verified": False,
            "training_rights_reviewed": False,
            "privacy_review_completed": False,
            "human_matching_labels": 0,
            "benchmark_approved": False,
        }
        records.append(meta)
        downloads.append((OUT / entry["filename"], raw))
    for path, raw in downloads:
        with path.open("xb") as file:
            file.write(raw)
        print("ORIGINAL", path.name, len(raw), hashlib.sha256(raw).hexdigest()[:16])
    with destination.open("x", encoding="utf-8") as file:
        json.dump({
            "schema_version": "public-procurement-source-v1",
            "entries": [*old_entries, *records],
            "training_approved": False,
        }, file, indent=2, ensure_ascii=False, sort_keys=True)
        file.write("\n")
    print("TOTAL_BUYER_DOCUMENTS",len(old_entries)+len(records))
    print("TRAINING_APPROVED",False)


if __name__ == "__main__":
    main()
