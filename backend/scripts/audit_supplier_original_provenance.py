"""Read-only integrity and provenance gate for 18 local supplier originals.

These are research candidate documents, not training-licensed gold labels.
Never modifies manifests or supplier source files.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from kpdesk.config import PROJECT_ROOT

_ROOT = PROJECT_ROOT / "ml/data/real"
_GROUPS: dict[str, int] = {
    "public_supplier_prices": 6,
    "research_20261021_supplier_prices": 6,
    "research_candidate_prices": 2,
    "research_unverified_supplier_prices": 4,
}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _verify_supplier_entry(
    directory: Path, row: dict[str, Any], known_digests: set[str],
    known_ids: set[str],
) -> dict[str, Any]:
    name = row.get("file")
    source_id = row.get("source_id")
    digest = row.get("sha256")
    size = row.get("bytes")
    if (
        not isinstance(name, str)
        or not name or Path(name).name != name
        or any(separator in name for separator in ("\\", "/", ":"))
        or not isinstance(source_id, str) or not source_id.strip()
        or source_id in known_ids
        or not isinstance(digest, str) or _SHA256.fullmatch(digest) is None
        or digest in known_digests
        or type(size) is not int or size < 1
    ):
        raise ValueError("Invalid or repeated supplier provenance fields")
    path = directory / name
    if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(
        directory.resolve()
    ):
        raise ValueError("Original supplier file absent or unsafe")
    if path.stat().st_size != size:
        raise ValueError("Original supplier byte size changed")
    measured = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            measured.update(chunk)
    if measured.hexdigest() != digest:
        raise ValueError("Original supplier SHA-256 differs from manifest")
    if row.get("model_training_authorized") is True or (
        row.get("training_authorized") is True
    ):
        raise ValueError("Research manifest unexpectedly asserts model training rights")
    known_digests.add(digest)
    known_ids.add(source_id)
    return {
        "source_id": source_id,
        "filename": name,
        "bytes": size,
        "sha256": digest,
        "model_training_authorized": False,
    }


def audit_supplier_originals(root: Path = _ROOT) -> dict[str, Any]:
    if not root.is_dir() or root.is_symlink():
        raise ValueError("Expected verified real supplier data root")
    seen_hashes: set[str] = set()
    seen_sources: set[str] = set()
    details: dict[str, list[dict[str, Any]]] = {}
    for group, expected_count in _GROUPS.items():
        directory = root / group
        manifest_path = directory / "manifest.json"
        if (
            not directory.is_dir() or directory.is_symlink()
            or not manifest_path.is_file() or manifest_path.is_symlink()
            or manifest_path.stat().st_size > 128_000
        ):
            raise ValueError("Supplier manifest absent or unsafe")
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise TypeError("Invalid supplier manifest structure")
        if group == "research_unverified_supplier_prices":
            main = document.get("source")
            additional = document.get("other_sources")
            if not isinstance(main, dict) or not isinstance(additional, list):
                raise ValueError("Unverified source manifest structure changed")
            entries = [main, *additional]
        else:
            raw_entries = document.get("sources")
            if not isinstance(raw_entries, list):
                raise ValueError("Supplier source list missing")
            entries = raw_entries
        if len(entries) != expected_count or (
            not all(isinstance(row, dict) for row in entries)
        ):
            raise ValueError("Supplier source count or structure changed")
        details[group] = [
            _verify_supplier_entry(directory, row, seen_hashes, seen_sources)
            for row in entries
        ]
    counts = Counter(Path(item["filename"]).suffix.casefold()
                     for entries in details.values() for item in entries)
    total = sum(len(entries) for entries in details.values())
    if total != 18 or len(seen_sources) != total or len(seen_hashes) != total:
        raise AssertionError("Incomplete independent supplier file inventory")
    return {
        "kind": "kpdesk_supplier_original_integrity_audit_v1",
        "source_files_checked": total,
        "unique_source_ids": len(seen_sources),
        "unique_sha256": len(seen_hashes),
        "declared_training_authorizations": 0,
        "source_formats": dict(sorted(counts.items())),
        "group_counts": {name: len(items) for name, items in details.items()},
        "training_dataset_eligible": False,
        "independent_labels_verified": False,
        "price_currency_unit_rights_not_inferred_from_hashes": True,
        "details": details,
    }
