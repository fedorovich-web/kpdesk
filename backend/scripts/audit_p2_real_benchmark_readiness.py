"""P2 real-request benchmark readiness, fail-closed and public-metadata-only.

Verify original source documents, excerpt provenance and manifest hash. Emit
aggregate counts, NOT request/source text, filenames, buyer IDs, or fake labels.
This never approves rights, reassigns groups, trains ML, or runs an LLM.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from kpdesk.matching.real_requests import RequestIntake, load_request_intake

ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = ROOT / "ml/data/real/public_procurement_requests"
INTAKE_PATH = SOURCE_ROOT / "intake-unreviewed-v3.json"
SOURCE_MANIFEST = SOURCE_ROOT / "manifest-v2.json"
_MIN_INDEPENDENT_GROUPS = 5
_MAX_MANIFEST_SIZE = 100_000


def _manifest(path: Path) -> dict[str, Any]:
    if (
        path.is_symlink() or not path.is_file()
        or not 0 < path.stat().st_size <= _MAX_MANIFEST_SIZE
    ):
        raise ValueError("Source manifest unavailable or unsafe")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != "public-procurement-source-v1"
        or value.get("training_approved") is not False
        or not isinstance(value.get("entries"), list)
    ):
        raise ValueError("Unrecognized or elevated source manifest")
    return value


def audit_readiness(
    intake: RequestIntake,
    manifest: Mapping[str, Any],
) -> dict[str, object]:
    """Fail closed if source identities or rights flags do not match evidence."""
    if (
        not isinstance(intake, RequestIntake)
        or not intake.source_excerpts_verified
        or intake.training_approved or intake.independently_verified
    ):
        raise ValueError("Request excerpts not trustworthy enough for an audit")
    entries = manifest.get("entries")
    if manifest.get("training_approved") is not False or not isinstance(entries, list):
        raise ValueError("Source manifest is not unapproved intake")
    records_by_file: dict[str, set[str]] = defaultdict(set)
    source_batches: set[str] = set()
    buyer_groups: dict[str, set[str]] = defaultdict(set)
    buyer_counts: Counter[str] = Counter()
    formats: Counter[str] = Counter()
    quantity_evidence_count = 0
    for record in intake.records:
        records_by_file[record.source_file].add(record.source_sha256)
        source_batches.add(record.source_batch_id)
        buyer_key = record.buyer_group_id or record.source_batch_id
        buyer_groups[buyer_key].add(record.source_batch_id)
        buyer_counts[buyer_key] += 1
        formats[Path(record.source_file).suffix.lower()] += 1
        quantity_evidence_count += record.quantity_evidence is not None
        if record.origin_declared != "public_procurement" or (
            record.rights_status != "review_required"
            or record.privacy_status != "review_required"
        ):
            raise ValueError("Unexpected source origin/permission escalation")
    providers: set[str] = set()
    accounted: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            raise TypeError("Invalid source manifest entry")
        filename = item.get("filename")
        if not isinstance(filename, str) or filename in accounted:
            raise ValueError("Duplicate source entry or non-string filename")
        accounted.add(filename)
        if records_by_file.get(filename) != {item.get("sha256")} or any(
            item.get(key) is not False for key in (
                "origin_independently_verified", "training_rights_reviewed",
                "privacy_review_completed", "benchmark_approved",
            )
        ) or item.get("human_matching_labels") != 0:
            raise ValueError("Source identity, permission, or label status mismatch")
        source_page = item.get("source_page")
        if not isinstance(source_page, str):
            raise TypeError("Source URL is unavailable")
        uri = urlparse(source_page)
        if uri.scheme != "https" or not uri.hostname or uri.username or uri.password:
            raise ValueError("Source URL is unsafe or non-HTTPS")
        providers.add(uri.hostname.casefold())
    if accounted != set(records_by_file):
        raise ValueError("Request dataset and source manifest cover different documents")
    buyers = len(buyer_groups)
    split_possible = buyers >= _MIN_INDEPENDENT_GROUPS
    blockers = [
        "reuse_rights_unverified",
        "request_privacy_review_missing",
        "human_sku_gold_missing",
        "supplier_catalog_cross_source_labels_missing",
        "llm_inference_and_token_cost_not_measured",
    ]
    if not split_possible:
        blockers.append("insufficient_independent_buyer_groups_for_3way_split")
    if len(providers) == 1:
        blockers.append("single_document_host_not_cross_provider_holdout")
    return {
        "schema_version": "p2-real-request-readiness-v1",
        "kind": "offline_aggregate_provenance_audit_not_model_evaluation",
        "input_intake_sha256": intake.source_sha256,
        "source_manifest_sha256": hashlib.sha256(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            .encode("utf-8")
        ).hexdigest(),
        "request_fragments": len(intake.records),
        "source_documents": len(accounted),
        "declared_source_batches": len(source_batches),
        "independent_buyer_groups": buyers,
        "source_documents_per_buyer_group_sorted": sorted(
            len(batch) for batch in buyer_groups.values()
        ),
        "request_fragments_per_buyer_group_sorted": sorted(buyer_counts.values()),
        "document_hosts": len(providers),
        "request_formats_by_extension": dict(sorted(formats.items())),
        "source_checked_adjacent_quantity_evidence": quantity_evidence_count,
        "minimum_independent_groups_required": _MIN_INDEPENDENT_GROUPS,
        "three_way_source_group_split_allowed": split_possible,
        "independently_authenticated_reviewer_labels": 0,
        "independent_source_disjoint_sku_gold": False,
        "rights_verified": False,
        "privacy_review_complete": False,
        "training_approved": False,
        "benchmark_approved": False,
        "llm_evaluation_performed": False,
        "blocked_reasons": blockers,
        "real_world_accuracy_metrics": None,
        "document_texts_or_buyer_identifiers_emitted": False,
    }


def build_report() -> dict[str, object]:
    return audit_readiness(
        load_request_intake(INTAKE_PATH, SOURCE_ROOT),
        _manifest(SOURCE_MANIFEST),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.is_symlink() or args.output.exists():
        parser.error("Will not overwrite or follow an existing output.")
    report = build_report()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as dest:
        json.dump(report, dest, ensure_ascii=False, sort_keys=True, indent=2)
        dest.write("\n")
    print(json.dumps({
        "request_fragments": report["request_fragments"],
        "source_documents": report["source_documents"],
        "independent_buyer_groups": report["independent_buyer_groups"],
        "three_way_source_group_split_allowed": report["three_way_source_group_split_allowed"],
        "benchmark_approved": False,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
