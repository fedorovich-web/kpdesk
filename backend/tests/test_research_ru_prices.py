"""Real but unapproved Russian price research: source, RUB and fail-closed exclusions."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from openpyxl import load_workbook

from scripts.audit_research_ru_prices import audit_research_ru_prices

CORPUS = Path(__file__).resolve().parents[2] / "ml/data/real/research_candidate_prices"


def test_additional_source_has_native_rub_product_prices_and_no_training_claim() -> None:
    results = audit_research_ru_prices(CORPUS)
    assert len(results) == 2
    aqualiga = next(x for x in results if x["source_id"] == "aqualiga_sanitary")
    krepish = next(x for x in results if x["source_id"] == "krepish_autofasteners")
    assert aqualiga["native_rub_verified"] is True
    assert aqualiga["candidate_product_rows"] == 3894
    assert aqualiga["format_profile"] == "five_columns_two_sheets_row_currency"
    assert krepish["native_rub_verified"] is False
    assert krepish["candidate_product_rows"] == 0
    assert krepish["exclusion_reason"] == "price_column_empty"
    assert all(x["current_price_verified"] is False for x in results)
    assert all(x["training_authorized"] is False for x in results)
    assert all(x["canonical_importable"] is False for x in results)


def test_additional_sources_refuse_non_rub_even_with_matching_sha(tmp_path: Path) -> None:
    import hashlib
    from shutil import copyfile

    folder = tmp_path / "sources"
    folder.mkdir()
    original = json.loads((CORPUS / "manifest.json").read_text(encoding="utf-8"))
    for item in original["sources"]:
        copyfile(CORPUS / item["file"], folder / item["file"])
    target = folder / "aqualiga_sanitary.xlsx"
    workbook = load_workbook(target)
    workbook.worksheets[0]["D2"] = "EUR"
    workbook.save(target)
    workbook.close()
    original["sources"][0]["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
    original["sources"][0]["bytes"] = target.stat().st_size
    (folder / "manifest.json").write_text(
        json.dumps(original, ensure_ascii=False), encoding="utf-8",
    )
    with pytest.raises(ValueError, match="RUB|currency"):
        audit_research_ru_prices(folder)


def test_additional_sources_refuse_foreign_seller(tmp_path: Path) -> None:
    import shutil

    shutil.copytree(CORPUS, tmp_path / "sources")
    manifest_file = tmp_path / "sources/manifest.json"
    data = json.loads(manifest_file.read_text(encoding="utf-8"))
    data["sources"][0]["source_page"] = "https://example.de/ware"
    manifest_file.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="host|source"):
        audit_research_ru_prices(tmp_path / "sources")
