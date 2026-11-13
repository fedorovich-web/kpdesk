"""Downloadable user templates must match current production catalog v2 schema."""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from openpyxl import load_workbook

from kpdesk.catalog.schema import COLUMNS_V2

TEMPLATES = Path(__file__).resolve().parents[2] / "frontend/public/templates"


def test_canonical_excel_downloadable_template_is_real_valid_xlsx() -> None:
    source = TEMPLATES / "kpdesk-catalog-v2-template.xlsx"
    content = source.read_bytes()
    assert content.startswith(b"PK\x03\x04")
    book = load_workbook(io.BytesIO(content), read_only=True, data_only=False)
    try:
        assert book.sheetnames == ["Products"]
        sheet = book["Products"]
        assert sheet.max_row == 1  # No fake/demonstration products.
        assert tuple(next(sheet.values)) == COLUMNS_V2
        assert sheet.max_column == len(COLUMNS_V2)
    finally:
        book.close()


def test_csv_template_header_is_exactly_v2_and_empty() -> None:
    content = (TEMPLATES / "kpdesk-catalog-v2-columns.csv").read_text(encoding="utf-8")
    rows = list(csv.reader(io.StringIO(content), delimiter=";"))
    assert rows == [list(COLUMNS_V2)]


def test_json_downloadable_template_has_explicit_unfilled_product() -> None:
    path = TEMPLATES / "kpdesk-catalog-v2-template.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == {"products"}
    assert isinstance(data["products"], list) and len(data["products"]) == 1
    row = data["products"][0]
    assert set(row) == set(COLUMNS_V2)
    assert not row["sku"] and not row["name"]
    assert row["price_rub"] is None
    assert row["attributes_json"] == {}
