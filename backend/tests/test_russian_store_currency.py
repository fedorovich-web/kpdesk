"""Only Russian online stores and explicitly RUB-denominated actual catalog prices."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from openpyxl import load_workbook

from kpdesk.catalog.russian_store_policy import inspect_russian_store_price
from scripts.audit_real_supplier_corpus import audit_real_supplier_corpus

ROOT = Path(__file__).resolve().parents[2] / "ml/data/real/public_supplier_prices"


def test_every_real_vendor_is_russian_store_and_xlsx_explicitly_rub() -> None:
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["sources"]) == 6
    audits = []
    for item in manifest["sources"]:
        result = inspect_russian_store_price(ROOT / item["file"], item)
        assert result["seller_country"] == "RU"
        assert result["price_currency"] == "RUB"
        assert result["store_market"] == "russian_online_store"
        assert result["currency_evidence"] in {"workbook_header_rub", "workbook_currency_statement_rub"}
        assert result["price_verified_current"] is False
        assert result["foreign_currency_conversion_permitted"] is False
        audits.append(result)
    assert sum(x["document_year"] == 2023 for x in audits) == 5
    assert sum(x["document_year"] == 2026 for x in audits) == 1
    assert sum(x["eligible_for_current_price_quotes"] is True for x in audits) == 0


def test_foreign_store_and_non_rub_currency_declined() -> None:
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    original = manifest["sources"][0]
    with pytest.raises(ValueError, match="Russian|store|host|country"):
        inspect_russian_store_price(ROOT / original["file"], {
            **original, "source_file_url": "https://example.de/produkte/price.xlsx",
        })
    with pytest.raises(ValueError, match="Russian|store|host|country"):
        inspect_russian_store_price(ROOT / original["file"], {
            **original, "source_page": "https://example.eu/store",
        })
    with pytest.raises(ValueError, match="currency|RUB"):
        inspect_russian_store_price(ROOT / original["file"], {
            **original, "currency": "EUR",
        })


def test_changed_workbook_currency_is_detected_even_if_checksum_updated(
    tmp_path: Path,
) -> None:
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    record = next(x for x in manifest["sources"] if x["source_id"] == "stroymaterialno_plumbing")
    workbook = load_workbook(ROOT / record["file"])
    sheet = workbook.worksheets[0]
    for row in sheet.iter_rows():
        for cell in row:
            if cell.value == "Цена (руб.)":
                cell.value = "Цена (EUR)"
                break
        else:
            continue
        break
    else:
        pytest.fail("Original RUB price header missing")
    target = tmp_path / record["file"]
    workbook.save(target)
    workbook.close()
    with pytest.raises(ValueError, match="currency|RUB"):
        inspect_russian_store_price(target, record, verify_sha=False)


def test_audit_includes_explicit_rub_and_blocks_historical_price_quotes() -> None:
    result = audit_real_supplier_corpus(ROOT)
    assert len(result) == 6
    for row in result:
        assert row["price_currency"] == "RUB"
        assert row["seller_country"] == "RU"
        assert row["eligible_for_current_price_quotes"] is False
        assert row["ready_for_supervised_training"] is False


def test_modified_foreign_source_manifest_is_rejected(tmp_path: Path) -> None:
    folder = tmp_path / "vendor"
    folder.mkdir()
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["sources"]:
        shutil.copyfile(ROOT / entry["file"], folder / entry["file"])
    manifest["sources"][0]["source_file_url"] = "https://example.fr/prix.xlsx"
    (folder / "manifest.json").write_text(json.dumps(manifest,ensure_ascii=False),encoding="utf-8")
    with pytest.raises(ValueError, match="Unrecognized|unapproved|Russian|store"):
        audit_real_supplier_corpus(folder)


def test_unreviewed_queue_fails_closed_when_source_is_not_rub_russian_store(
    tmp_path: Path,
) -> None:
    from kpdesk.matching.review_queue import build_review_queue

    copy = tmp_path / "source"
    copy.mkdir()
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["sources"]:
        shutil.copyfile(ROOT / entry["file"], copy / entry["file"])
    manifest["sources"][0]["currency"] = "EUR"
    (copy / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8",
    )
    with pytest.raises(ValueError, match="currency|RUB"):
        build_review_queue(copy, per_source=1, candidate_limit=2)


def test_russian_store_snapshot_lists_only_ru_rub_and_keeps_origins(tmp_path: Path) -> None:
    from scripts.audit_russian_price_sources import build_russian_price_audit, main

    audit = build_russian_price_audit(ROOT)
    assert audit["audited_source_count"] == 6
    assert audit["audited_product_candidates"] == 2436
    assert audit["require_seller_country"] == "RU"
    assert audit["require_native_currency"] == "RUB"
    assert audit["allow_foreign_currency_conversion"] is False
    assert audit["ready_for_real_supervised_training"] is False
    assert isinstance(audit["sources"], list)
    assert {source["source_host"] for source in audit["sources"]} == {
        "stroymaterialno.ru", "www.svihcompany.ru",
    }
    dest = tmp_path / "ru-policy.json"
    assert main(["--corpus", str(ROOT), "--output", str(dest)]) == 0
    assert json.loads(dest.read_text(encoding="utf-8"))["sources"] == audit["sources"]
    with pytest.raises(FileExistsError):
        main(["--corpus", str(ROOT), "--output", str(dest)])
