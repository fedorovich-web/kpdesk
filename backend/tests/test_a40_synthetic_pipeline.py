"""Synthetic HTTP matching-to-quote smoke without touching the active local catalog.

HTTP routes use the existing in-memory repository dependency override. The
models and matching/quote/PDF services are real; no Groq, DB writes, or mock
prices are accepted from the model.
"""
from __future__ import annotations

import csv
import io
import uuid

from fastapi.testclient import TestClient
from test_catalog_api import FakeCatalogRepository

from kpdesk.api.routes import get_catalog_repository
from kpdesk.catalog.schema import COLUMNS_V2
from kpdesk.config import AppSettings
from kpdesk.extraction.schema import ExtractedRequest
from kpdesk.main import create_app
from kpdesk.quotes.pdf import render_draft_pdf
from kpdesk.quotes.storage import ManagerSkuSelection, StoredQuote, _make_snapshot

ORIGIN = {"Origin": "http://127.0.0.1:5173"}


def _csv_catalog(rows: list[tuple[str, str, str, str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS_V2, delimiter=";",
                            lineterminator="\n")
    writer.writeheader()
    for sku, name, price, paper_format in rows:
        writer.writerow({
            "sku": sku,
            "name": name,
            "category": "paper",
            "sale_unit": "пачка",
            "pieces_per_sale_unit": 1,
            "price_rub": price,
            "paper_format": paper_format,
            "paper_gsm": 80,
            "attributes_json": "{}",
        })
    return buffer.getvalue().encode("utf-8")


def _line() -> dict[str, object]:
    return {
        "source_quote": "Бумага А4 80 г, две пачки",
        "description": "Бумага А4 80 г",
        "category": "paper",
        "quantity": 2,
        "unit": "пачка",
        "brand": None,
        "paper_format": "A4",
        "paper_gsm": 80,
        "pen_ink_color": None,
        "pen_ink_type": None,
        "attributes": {},
    }


def test_synthetic_import_ml_preview_manager_price_lock_and_pdf() -> None:
    repository = FakeCatalogRepository()
    app = create_app(
        testing=True, settings=AppSettings(enable_synthetic_ml=True),
    )
    app.dependency_overrides[get_catalog_repository] = lambda: repository
    with TestClient(app) as client:
        upload = client.post(
            "/api/catalog/import",
            headers={**ORIGIN, "Idempotency-Key": "synthetic-a40"},
            files={"file": ("synthetic.csv", _csv_catalog([
                ("P-1", "Бумага А4 80 г", "249.50", "A4"),
                ("P-2", "Бумага A4 офисная 80", "300.00", "A4"),
                ("P-3", "Бумага A3", "150.00", "A3"),
            ]), "text/csv")},
        )
        assert upload.status_code == 201, upload.text
        catalog_id = upload.json()["id"]
        assert client.get("/api/capabilities").json()["model_trained"] is True

        suggestion = client.post(
            "/api/matches/category-suggestion",
            json={"description": _line()["description"], "original_category": "paper"},
            headers=ORIGIN,
        )
        assert suggestion.status_code == 200, suggestion.text
        assert suggestion.json()["original_category"] == "paper"
        assert suggestion.json()["requires_explicit_acceptance"] is True

        preview = client.post(
            "/api/matches/preview-extracted",
            json={"extracted_line": _line(), "mode": "ml", "limit": 20},
            headers=ORIGIN,
        )
        assert preview.status_code == 200, preview.text
        data = preview.json()
        assert data["catalog_version_id"] == catalog_id
        assert data["requires_manager_confirmation"] is True
        by_sku = {candidate["sku"]: candidate for candidate in data["candidates"]}
        assert set(by_sku) == {"P-1", "P-2"}
        assert by_sku["P-2"]["status"] == "compatible"
        assert by_sku["P-2"]["price_rub"] == "300.00"
        assert all(candidate["requires_manager_confirmation"] for candidate in by_sku.values())

        extracted = ExtractedRequest.model_validate({"lines": [_line()]})
        products = repository.items(uuid.UUID(catalog_id))
        unselected = _make_snapshot(extracted, products)
        assert unselected.ready_for_draft is False
        assert unselected.total_rub is None

        selected = _make_snapshot(
            extracted, products, [ManagerSkuSelection(line_number=1, sku="P-2")],
        )
        assert selected.ready_for_draft
        assert selected.lines[0].status == "manager_selected"
        assert selected.lines[0].unit_price_rub == 300
        assert selected.total_rub == 600
        quote = StoredQuote(
            quote_id=uuid.uuid4(), revision=1,
            catalog_version_id=uuid.UUID(catalog_id),
            ready_for_draft=True, total_rub=selected.total_rub,
            lines=selected.lines, snapshot_sha256="0" * 64,
        )
        assert render_draft_pdf(quote).startswith(b"%PDF-")

        no_match = client.post(
            "/api/matches/preview",
            json={"requested_item": {
                "description": "Беспроводная мышь", "category": "mouse",
            }, "mode": "ml"},
            headers=ORIGIN,
        )
        assert no_match.status_code == 200, no_match.text
        assert no_match.json()["candidates"] == []

        # A price-list replacement never makes old previews authoritative.
        newer = client.post(
            "/api/catalog/import",
            headers={**ORIGIN, "Idempotency-Key": "synthetic-a40-next"},
            files={"file": ("changed.csv", _csv_catalog([
                ("P-4", "Бумага А4 80 г", "380.00", "A4"),
            ]), "text/csv")},
        )
        assert newer.status_code == 201, newer.text
        assert newer.json()["id"] != catalog_id
        from pytest import raises

        with raises(ValueError, match="Selected SKU is missing"):
            _make_snapshot(
                extracted,
                repository.items(uuid.UUID(newer.json()["id"])),
                [ManagerSkuSelection(line_number=1, sku="P-2")],
            )
