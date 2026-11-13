"""Real (unlabeled) public supplier prices: local integrity and truthful provenance."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.audit_real_supplier_corpus import audit_real_supplier_corpus

SOURCE_DIR = Path(__file__).resolve().parents[2] / "ml/data/real/public_supplier_prices"


def test_real_corpus_contains_six_downloaded_vendor_originals_not_synthetic_gold() -> None:
    summary = audit_real_supplier_corpus(SOURCE_DIR)
    assert len(summary) == 6
    assert len({item["source_id"] for item in summary}) == 6
    assert sum(int(item["candidate_product_rows"]) for item in summary) >= 1000
    assert all(int(item["candidate_product_rows"]) >= 25 for item in summary)
    assert {item["source_host"] for item in summary} == {
        "www.svihcompany.ru", "stroymaterialno.ru",
    }
    assert all(item["integrity_verified"] is True for item in summary)
    assert all(item["ready_for_supervised_training"] is False for item in summary)
    assert sum(int(item["source_year_2023"]) for item in summary) == 5


def test_real_corpus_manifest_does_not_call_public_catalogs_reviewed_labels() -> None:
    data = json.loads((SOURCE_DIR / "manifest.json").read_text(encoding="utf-8"))
    entries = data["sources"]
    assert len(entries) == 6
    assert all(entry["provenance"] == "real_public_supplier_file" for entry in entries)
    assert all(entry["labels"] == "not_human_reviewed" for entry in entries)
    assert all(entry["model_training_authorized"] is False for entry in entries)
    assert all(entry["prices_verified_current"] is False for entry in entries)
    assert all(entry["canonical_importable"] is False for entry in entries)
    assert all("first_four_rows" not in entry for entry in entries)


def test_real_corpus_integrity_fails_on_modified_manifest_copy(tmp_path: Path) -> None:
    import shutil
    copy = tmp_path / "test_corpus"
    copy.mkdir()
    manifest = json.loads((SOURCE_DIR / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["sources"]:
        shutil.copyfile(SOURCE_DIR / entry["file"], copy / entry["file"])
    (copy / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8",
    )
    source = copy / manifest["sources"][0]["file"]
    source.write_bytes(source.read_bytes() + b"not_original")
    with pytest.raises(ValueError, match="checksum"):
        audit_real_supplier_corpus(copy)
