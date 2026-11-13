"""Create-only, bounded acquisition of two publicly linked 2026 buyer specifications.

Explicit two-file allowlist; local format/evidence research only.
No training permission inferred, no authentication or broad crawling.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zipfile import ZipFile

PROJECT = Path(__file__).resolve().parents[2]
OUT = PROJECT / "ml/data/real/public_procurement_requests"
LIMIT = 2_000_000
SOURCES = (
    {
        "id": "bidzaar_372_651_stationery_2026",
        "source_page": "https://bidzaar.com/app/process/light/01a08542-b7f9-7488-874c-043af141e79f",
        "document_url": "https://bidzaar.com/api/filestorage/files/download/bf21985e-c22b-4db6-a833-a89f53064d01",
        "filename": "bidzaar_372_651_stationery_2026.docx",
        "advertised_document_date": "2026-09-09",
    },
    {
        "id": "bidzaar_339_280_stationery_2026",
        "source_page": "https://bidzaar.com/app/process/light/019ea5c7-b6df-7f96-a755-b08df7f7c558",
        "document_url": "https://bidzaar.com/api/filestorage/files/download/40ec4fa3-5a2e-4e0c-996f-290e36b17c99",
        "filename": "bidzaar_339_280_stationery_2026.xlsx",
        "advertised_document_date": "2026-06-10",
    },
)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    manifest_path = OUT / "manifest.json"
    if manifest_path.exists() or any((OUT / item["filename"]).exists() for item in SOURCES):
        raise FileExistsError("Existing procurement originals or manifest must not be overwritten.")
    entries = []
    for source in SOURCES:
        link = source["document_url"]
        if urlparse(link).scheme != "https" or urlparse(link).hostname != "bidzaar.com":
            raise ValueError("Unexpected source endpoint.")
        with urlopen(Request(link, headers={
            "User-Agent": "KPDesk/0.1 (offline procurement structure research; support@kpdesk.ru)"
        }), timeout=12) as response:
            if (urlparse(response.geturl()).scheme != "https"
                    or urlparse(response.geturl()).hostname != "bidzaar.com"):
                raise ValueError("Redirected away from allowlisted supplier site.")
            content = response.read(LIMIT + 1)
        if not 100 <= len(content) <= LIMIT or not content.startswith(b"PK"):
            raise ValueError("Unexpected/non-OOXML file or oversized remote document.")
        with ZipFile(BytesIO(content)) as archive:
            paths = set(archive.namelist())
            required = ("xl/workbook.xml" if source["filename"].endswith(".xlsx")
                        else "word/document.xml")
            if required not in paths or sum(x.file_size for x in archive.infolist()) > 50_000_000:
                raise ValueError("OOXML source missing or expanded too large.")
        (OUT / source["filename"]).write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        meta = {
            **source,
            "retrieved_at_utc": datetime.now(UTC).isoformat(),
            "sha256": digest,
            "bytes": len(content),
            "provenance_declared": "published_public_buyer_procurement_request",
            "origin_independently_verified": False,
            "training_rights_reviewed": False,
            "privacy_review_completed": False,
            "human_matching_labels": 0,
            "benchmark_approved": False,
        }
        entries.append(meta)
        print("ACQUIRED", source["id"], len(content), digest[:16])
    with manifest_path.open("x", encoding="utf-8") as output:
        json.dump({"schema_version":"public-procurement-source-v1", "entries":entries,
                   "training_approved":False},output,ensure_ascii=False,indent=2)
    print("SOURCE_MANIFEST",str(manifest_path),"SOURCE_FILES",len(entries))


if __name__ == "__main__":
    main()
