from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from scripts.audit_supplier_original_provenance import (
    _verify_supplier_entry,
    audit_supplier_originals,
)


def test_supplier_original_file_provenance() -> None:
    report = audit_supplier_originals()
    assert report["source_files_checked"] == 18
    assert report["unique_source_ids"] == 18
    assert report["unique_sha256"] == 18
    assert report["training_dataset_eligible"] is False
    assert report["declared_training_authorizations"] == 0
    assert report["group_counts"] == {
        "public_supplier_prices": 6,
        "research_20261021_supplier_prices": 6,
        "research_candidate_prices": 2,
        "research_unverified_supplier_prices": 4,
    }


def test_source_metadata_and_file_integrity_require_exact_match(tmp_path: Path) -> None:
    contents = b"sku;price\n001;300\n"
    original = tmp_path / "fixture.csv"
    original.write_bytes(contents)
    row: dict[str, object] = {
        "file": "fixture.csv", "source_id": "sample-1",
        "sha256": hashlib.sha256(contents).hexdigest(),
        "bytes": len(contents), "model_training_authorized": False,
    }
    result = _verify_supplier_entry(tmp_path, row, set(), set())
    assert result["source_id"] == "sample-1"
    with pytest.raises(ValueError):
        _verify_supplier_entry(tmp_path, {**row, "bytes": 1000}, set(), set())
    with pytest.raises(ValueError):
        _verify_supplier_entry(tmp_path, {**row, "file": "../fixture.csv"}, set(), set())
    with pytest.raises(ValueError):
        _verify_supplier_entry(
            tmp_path, {**row, "model_training_authorized": True}, set(), set(),
        )
    with pytest.raises(ValueError):
        _verify_supplier_entry(
            tmp_path, row, {str(row["sha256"])}, set(),
        )
