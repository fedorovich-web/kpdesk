"""Evidence-only local buyer-specification excerpts from TWO genuine 2026 tenders.

Human-reviewed gold: none. Dataset permissions/privacy/origin: NOT independently
verified. No generated queries, SKU labels, network calls, training or DB writes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import urlparse

from kpdesk.matching.real_requests import RequestIntakeRecord, load_request_intake
from kpdesk.matching.source_evidence import (
    AUDITED_XLSX_QUANTITY_LAYOUTS,
    extract_source_spans,
    extract_xlsx_row_amount,
    locate_source_excerpt,
)

ROOT = Path(__file__).resolve().parents[2] / "ml/data/real/public_procurement_requests"
DOCX_LOCATIONS = frozenset({
    20, 27, 30, 35, 67, 80, 85, 95, 110, 125, 130, 142, 150,
})
XLSX_ROWS = frozenset(range(5, 16))
FILES = {
    "bidzaar_372_651_stationery_2026": "bidzaar_372_651_stationery_2026.docx",
    "bidzaar_339_280_stationery_2026": "bidzaar_339_280_stationery_2026.xlsx",
    "bidzaar_284960_paper_2026": "bidzaar_284960_paper_2026.docx",
    "bidzaar_337279_design_2026": "bidzaar_337279_design_2026.xlsx",
    "bidzaar_364642_tmc_2026": "bidzaar_364642_tmc_2026.xlsx",
}
BUYER_GROUPS = {
    "bidzaar_372_651_stationery_2026": "akron_holding",
    "bidzaar_339_280_stationery_2026": "akron_holding",
    "bidzaar_284960_paper_2026": "megapolis",
    "bidzaar_337279_design_2026": "salut_devices",
    "bidzaar_364642_tmc_2026": "mvm",
}
AUDITED_LOCATIONS: dict[str, frozenset[str]] = {
    "bidzaar_372_651_stationery_2026.docx": frozenset(
        f"paragraph:{p}" for p in DOCX_LOCATIONS
    ),
    "bidzaar_284960_paper_2026.docx": frozenset({"paragraph:13"}),
    "bidzaar_339_280_stationery_2026.xlsx": frozenset(f"C{p}" for p in XLSX_ROWS),
    "bidzaar_337279_design_2026.xlsx": frozenset(f"C{p}" for p in range(4, 20)),
    "bidzaar_364642_tmc_2026.xlsx": frozenset(f"B{p}" for p in range(12, 24)),
}


def _selected_location(filename: str, location: str) -> bool:
    expected = AUDITED_LOCATIONS[filename]
    if filename.endswith(".docx"):
        return location in expected
    if filename == "bidzaar_364642_tmc_2026.xlsx" and not location.startswith(
        "sheet:Спецификация!"
    ):
        return False  # Other sheets contain delivery-location/recipient data.
    return location.startswith("sheet:") and location.rsplit("!", 1)[-1] in expected


def build_unreviewed_procurement_intake(
    sources: Path, *, manifest_name: str = "manifest.json",
) -> dict[str, Any]:
    if manifest_name not in ("manifest.json", "manifest-v2.json"):
        raise ValueError("Unknown public procurement manifest version.")
    manifest_file = sources / manifest_name
    if not manifest_file.is_file() or manifest_file.stat().st_size > 100_000:
        raise ValueError("Public buyer-source provenance manifest missing.")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if (manifest.get("schema_version") != "public-procurement-source-v1"
            or manifest.get("training_approved") is not False):
        raise ValueError("Unexpected buyer-source provenance or rights status.")
    entries = manifest.get("entries")
    expected_source_count = 2 if manifest_name == "manifest.json" else 5
    if not isinstance(entries, list) or len(entries) != expected_source_count:
        raise ValueError("Unexpected procurement source document count.")
    records: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    for item in entries:
        source_id = item.get("id")
        if source_id not in FILES or source_id in seen_sources:
            raise ValueError("Unexpected or duplicated buyer-source identity.")
        seen_sources.add(source_id)
        filename = FILES[source_id]
        if (
            item.get("filename") != filename
            or item.get("origin_independently_verified") is not False
            or item.get("training_rights_reviewed") is not False
            or item.get("privacy_review_completed") is not False
            or item.get("benchmark_approved") is not False
            or item.get("human_matching_labels") != 0
            or urlparse(item.get("source_page", "")).hostname != "bidzaar.com"
            or urlparse(item.get("document_url", "")).hostname != "bidzaar.com"
        ):
            raise ValueError("Source manifest contains unsupported provenance assertions.")
        source = sources / filename
        if source.is_symlink() or not source.is_file() or not 1 <= source.stat().st_size <= 2_000_000:
            raise ValueError("Missing or unsafe original procurement document.")
        binary = source.read_bytes()
        digest = hashlib.sha256(binary).hexdigest()
        if digest != item.get("sha256") or len(binary) != item.get("bytes"):
            raise ValueError("Buyer source original SHA256 or size changed.")
        for location, quote in extract_source_spans(source, binary):
            if not _selected_location(filename, location):
                continue
            if not 12 <= len(quote.strip()) <= 2000:
                raise ValueError(
                    f"Selected buyer specification line outside limits at {filename}:{location}."
                )
            verified_location = locate_source_excerpt(
                source, quote, expected_sha256=digest,
            )
            if verified_location != location:
                raise ValueError("Source span ambiguous or shifted since acquisition.")
            key = hashlib.sha256(
                f"{source_id}:{location}:{digest}".encode()
            ).hexdigest()[:20]
            layout = AUDITED_XLSX_QUANTITY_LAYOUTS.get(filename)
            quantity_evidence = (
                extract_xlsx_row_amount(
                    source, quote, expected_sha256=digest,
                    title_column=layout[0], quantity_column=layout[1], unit_column=layout[2],
                ) if layout is not None else None
            )
            row = RequestIntakeRecord.model_validate({
                "quantity_evidence": quantity_evidence,
                "buyer_group_id": BUYER_GROUPS[source_id],
                "request_id": "buyer-" + key,
                "origin_declared": "public_procurement",
                "source_batch_id": source_id,
                "source_file": filename,
                "source_sha256": digest,
                "source_quote": quote,
                "request_text": quote,
                "captured_at": item["retrieved_at_utc"],
                "rights_status": "review_required",
                "privacy_status": "review_required",
            })
            records.append(row.model_dump(mode="json"))
    if not (20 <= len(records) <= 80):
        raise ValueError("Tender source documents changed; review selected locations.")
    return {"schema_version": "real-request-intake-v1", "records": records}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verified verbatim public buyer request excerpts.")
    parser.add_argument("--sources", type=Path, default=ROOT)
    parser.add_argument("--manifest-name", choices=("manifest.json", "manifest-v2.json"),
                        default="manifest.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    dataset = build_unreviewed_procurement_intake(
        args.sources, manifest_name=args.manifest_name,
    )
    body = json.dumps(dataset, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    # Validate the complete source-backed intake before publishing a create-only file.
    with TemporaryDirectory(prefix="kpdesk-procurement-verify-") as staging:
        staged = Path(staging) / "intake.json"
        staged.write_text(body, encoding="utf-8")
        checked = load_request_intake(staged, args.sources)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(body)
    print(json.dumps({
        "request_excerpts": len(checked.records),
        "source_files": len({row.source_file for row in checked.records}),
        "source_groups": len({row.source_batch_id for row in checked.records}),
        "intake_sha256": checked.source_sha256,
        "verbatim_source_excerpts_verified": checked.source_excerpts_verified,
        "independently_verified": False,
        "training_approved": False,
        "human_labels": 0,
        "locked_test_eligible": False,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
