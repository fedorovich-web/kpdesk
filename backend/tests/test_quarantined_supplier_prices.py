from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from openpyxl import load_workbook

from scripts.audit_quarantined_supplier_prices import (
    EXPECTED_SOURCES,
    _audit_krepiz_file,
    _audit_virsan_file,
    audit_quarantined_supplier_prices,
)

CORPUS = (
    Path(__file__).resolve().parents[2]
    / "ml/data/real/research_unverified_supplier_prices"
)


def _copy_corpus(destination: Path) -> Path:
    destination.mkdir()
    shutil.copyfile(CORPUS / "manifest.json", destination / "manifest.json")
    for source in EXPECTED_SOURCES.values():
        shutil.copyfile(CORPUS / source.filename, destination / source.filename)
    return destination


def test_quarantined_corpus_audits_native_rub_structure_without_approving_use() -> None:
    audit = audit_quarantined_supplier_prices(CORPUS)
    assert audit["source_count"] == 4
    assert audit["structurally_audited_native_rub_sources"] == 2
    assert audit["candidate_price_rows_verified"] == 32_947
    assert audit["current_prices_verified"] is False
    assert audit["reuse_rights_reviewed"] is False
    assert audit["canonical_importable"] is False
    assert audit["training_authorized"] is False

    source_records = audit["sources"]
    assert isinstance(source_records, list)
    sources: dict[str, dict[str, object]] = {}
    for item in source_records:
        assert isinstance(item, dict)
        source_id = item.get("source_id")
        assert isinstance(source_id, str)
        sources[source_id] = item
    krepiz = sources["krepiz_fasteners_2026"]
    assert krepiz["candidate_price_rows_verified"] == 26_916
    assert krepiz["price_tier_count"] == 6
    assert krepiz["unique_sku_count"] == 26_915
    assert krepiz["duplicate_sku_groups"] == 1
    assert krepiz["incomplete_price_rows"] == 0
    assert krepiz["native_rub_verified"] is True
    assert krepiz["training_authorized"] is False

    virsan = sources["virsan_sanitary_20260915"]
    assert virsan["product_identity_rows"] == 6_035
    assert virsan["candidate_price_rows_verified"] == 6_031
    assert virsan["incomplete_price_rows"] == 4
    assert virsan["price_tier_count"] == 3
    assert virsan["unique_sku_count"] == 6_035
    assert virsan["duplicate_sku_groups"] == 0
    assert virsan["native_rub_verified"] is True
    assert virsan["current_price_verified"] is False


def test_quarantined_corpus_rejects_changed_source_bytes(tmp_path: Path) -> None:
    copied = _copy_corpus(tmp_path / "supplier-corpus")
    source = copied / "krepiz_fasteners_2026.xlsx"
    source.write_bytes(source.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="checksum|byte count"):
        audit_quarantined_supplier_prices(copied)


def test_quarantined_corpus_rejects_a_source_url_rewrite(tmp_path: Path) -> None:
    copied = _copy_corpus(tmp_path / "supplier-corpus")
    manifest_path = copied / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    krepiz = next(
        item for item in manifest["other_sources"]
        if item["source_id"] == "krepiz_fasteners_2026"
    )
    krepiz["source_page"] = "https://example.com/price/"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="metadata changed"):
        audit_quarantined_supplier_prices(copied)


def test_quarantined_corpus_cannot_promote_a_source_to_training(tmp_path: Path) -> None:
    copied = _copy_corpus(tmp_path / "supplier-corpus")
    manifest_path = copied / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    krepiz = next(
        item for item in manifest["other_sources"]
        if item["source_id"] == "krepiz_fasteners_2026"
    )
    krepiz["model_training_authorized"] = True
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="cannot be promoted"):
        audit_quarantined_supplier_prices(copied)


def test_manifest_cannot_silently_change_audited_price_counts(tmp_path: Path) -> None:
    copied = _copy_corpus(tmp_path / "supplier-corpus")
    manifest_path = copied / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    virsan = next(
        item for item in manifest["other_sources"]
        if item["source_id"] == "virsan_sanitary_20260915"
    )
    virsan["price_rows_verified"] = 6_030
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="audit metadata mismatch"):
        audit_quarantined_supplier_prices(copied)


def test_krepiz_requires_explicit_currency_and_order_tier_headers(tmp_path: Path) -> None:
    source = CORPUS / "krepiz_fasteners_2026.xlsx"
    copied = tmp_path / "modified_krepiz.xlsx"
    shutil.copyfile(source, copied)
    workbook = load_workbook(copied)
    workbook.worksheets[0]["D3"] = "от 120 до 200 thousand"
    workbook.save(copied)
    workbook.close()
    with pytest.raises(ValueError, match="headers or price-tier"):
        _audit_krepiz_file(copied)


def test_virsan_price_audit_excludes_rows_without_all_price_tiers() -> None:
    source = CORPUS / "virsan_sanitary_20260915.xls"
    audit = _audit_virsan_file(source)
    assert audit["native_rub_verified"] is True
    assert audit["product_identity_rows"] == 6_035
    assert audit["candidate_price_rows_verified"] == 6_031
    assert audit["incomplete_price_rows"] == 4
    assert audit["price_tier_count"] == 3
    assert audit["training_authorized"] is False
