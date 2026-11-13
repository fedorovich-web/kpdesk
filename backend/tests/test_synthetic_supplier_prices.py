"""Supplier price fixtures are synthetic, diverse, bounded and reproducible."""

import hashlib
import json
from pathlib import Path

from openpyxl import load_workbook

from kpdesk.matching.profiles import CATEGORY_ATTRIBUTES
from scripts.build_synthetic_supplier_prices import CASES, build_corpus


def test_twenty_suppliers_have_distinct_taxonomy_and_source_schemas(tmp_path: Path) -> None:
    assert len(CASES) == 20
    assert len({case.case_id for case in CASES}) == 20
    assert len({case.category for case in CASES}) == 20
    assert all(case.category in CATEGORY_ATTRIBUTES for case in CASES[:8])
    assert all(case.category not in CATEGORY_ATTRIBUTES for case in CASES[8:])
    first = tmp_path / "first"
    manifest = build_corpus(first)
    assert manifest["provenance"] == "synthetic"
    assert manifest["gold_review_status"] == "not_human_reviewed"
    assert len(manifest["cases"]) == 20
    assert len({entry["sha256"] for entry in manifest["cases"]}) == 20
    for entry in manifest["cases"]:
        target = first / entry["file"]
        assert target.is_file()
        assert len(target.read_bytes()) <= 10 * 1024 * 1024
        assert hashlib.sha256(target.read_bytes()).hexdigest() == entry["sha256"]
        assert entry["row_count"] >= 40
        assert entry["canonical_importable"] is False
        assert entry["taxonomy_supported"] is (int(entry["case_id"]) <= 8)
        assert entry["provenance"] == "synthetic"
        if target.suffix == ".xlsx":
            book = load_workbook(target, read_only=True, data_only=False)
            try:
                assert entry["sheet"] in book.sheetnames
                assert book[entry["sheet"]].max_row >= entry["row_count"]
            finally:
                book.close()
    assert json.loads((first / "manifest.json").read_text(encoding="utf-8")) == manifest
    second = tmp_path / "second"
    again = build_corpus(second)
    assert again == manifest
    for entry in manifest["cases"]:
        assert (first / entry["file"]).read_bytes() == (second / entry["file"]).read_bytes()


def test_supplier_dialects_do_not_bypass_strict_catalog_import(tmp_path: Path) -> None:
    from kpdesk.catalog.parse import parse_price_list
    from kpdesk.catalog.schema import ImportLimits

    base = tmp_path / "raw"
    manifest = build_corpus(base)
    for entry in manifest["cases"]:
        raw = (base / entry["file"]).read_bytes()
        result = parse_price_list(entry["file"], raw, ImportLimits())
        assert not result.publishable, entry["case_id"]
        assert result.products == (), entry["case_id"]
        assert result.errors, entry["case_id"]
