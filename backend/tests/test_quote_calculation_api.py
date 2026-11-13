"""HTTP draft-calculation contract: authoritative catalog pricing, no approval."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from test_catalog_api import FakeCatalogRepository

from kpdesk.api.routes import get_catalog_repository
from kpdesk.catalog.storage import PublishedCatalog
from kpdesk.main import create_app
from kpdesk.matching.schema import CatalogProduct

HEADERS = {"Origin": "http://localhost:5173"}
REQUEST = {
    "description": "11 шариковых ручек синих", "category": "pen",
    "quantity": 11, "unit": "штука",
    "pen_ink_color": "синий", "pen_ink_type": "шариковая",
}
EXTRACTED = {
    "source_quote": "беспроводные мыши 1600 DPI",
    "description": "Мышь беспроводная 1600 DPI",
    "category": "mouse", "quantity": 2, "unit": "штука",
    "brand": None, "paper_format": None, "paper_gsm": None,
    "pen_ink_color": None, "pen_ink_type": None,
    "attributes": {"connection": "wireless", "dpi": 1600},
}


@pytest.fixture
def client_and_version() -> tuple[TestClient, str]:
    version = PublishedCatalog(id=uuid.uuid4(), source_sha256="b" * 64, rows_count=3)
    repo = FakeCatalogRepository(
        published=version,
        products=(
            CatalogProduct(
                sku="P-1", name="Ручка синяя шариковая",
                category="pen", sale_unit="коробка",
                pieces_per_sale_unit=10, price_rub=Decimal("125.00"),
                pen_ink_color="синий", pen_ink_type="шариковая",
            ),
            CatalogProduct(
                sku="M-1", name="Мышь беспроводная 1600 DPI",
                category="mouse", sale_unit="штука", pieces_per_sale_unit=1,
                price_rub=Decimal("496.51"),
                attributes={"connection": "wireless", "dpi": 1600},
            ),
            CatalogProduct(
                sku="M-2", name="Мышь проводная 1600 DPI",
                category="mouse", sale_unit="штука", pieces_per_sale_unit=1,
                price_rub=Decimal("340.00"),
                attributes={"connection": "wired", "dpi": 1600},
            ),
        ),
    )
    app = create_app(testing=True)
    app.dependency_overrides[get_catalog_repository] = lambda: repo
    return TestClient(app), str(version.id)


def test_direct_groq_line_to_matching_preview_without_fake_approval(
    client_and_version: tuple[TestClient, str],
) -> None:
    client, _ = client_and_version
    response = client.post(
        "/api/matches/preview-extracted",
        json={"extracted_line": EXTRACTED, "mode": "rapidfuzz"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    assert [c["sku"] for c in response.json()["candidates"]] == ["M-1"]
    assert response.json()["requires_manager_confirmation"] is True


def test_unknown_extracted_category_requires_manual_classification(
    client_and_version: tuple[TestClient, str],
) -> None:
    client, _ = client_and_version
    response = client.post(
        "/api/matches/preview-extracted",
        json={"extracted_line": {**EXTRACTED, "category": None, "attributes": {}}},
        headers=HEADERS,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "category_needs_review"


def test_rounding_required_results_in_incomplete_quote_and_null_total(
    client_and_version: tuple[TestClient, str],
) -> None:
    client, version = client_and_version
    payload = {
        "catalog_version_id": version,
        "lines": [{"sku": "P-1", "requested_item": REQUEST}],
    }
    response = client.post("/api/quotes/calculate", json=payload, headers=HEADERS)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["ready"] is False
    assert result["total_rub"] is None
    assert result["lines"][0]["suggested_sale_quantity"] == 2
    assert result["lines"][0]["line_total_rub"] is None
    assert result["lines"][0]["status"] == "needs_review"


def test_approved_explicit_overage_calculates_from_database_price(
    client_and_version: tuple[TestClient, str],
) -> None:
    client, version = client_and_version
    payload = {
        "catalog_version_id": version,
        "lines": [{
            "sku": "P-1", "requested_item": REQUEST,
            "approved_sale_quantity": 2,
        }],
    }
    response = client.post("/api/quotes/calculate", json=payload, headers=HEADERS)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["ready"] is True
    assert result["total_rub"] == "250.00"
    assert result["lines"][0]["overage_pieces"] == 9
    assert result["requires_manager_confirmation"] is True
    assert "approved" not in result


def test_incompatible_product_stale_catalog_and_missing_qty_fail_closed(
    client_and_version: tuple[TestClient, str],
) -> None:
    client, version = client_and_version
    requests = [
        ({"catalog_version_id": str(uuid.uuid4()), "lines": [{
            "sku": "P-1", "requested_item": REQUEST,
        }]}, 409, "stale_catalog"),
        ({"catalog_version_id": version, "lines": [{
            "sku": "M-1", "requested_item": REQUEST,
        }]}, 422, "invalid_quote_line"),
        ({"catalog_version_id": version, "lines": [{
            "sku": "P-1", "requested_item": {**REQUEST, "quantity": None},
        }]}, 422, "invalid_quote_line"),
    ]
    for payload, code, expected in requests:
        response = client.post("/api/quotes/calculate", json=payload, headers=HEADERS)
        assert response.status_code == code, response.text
        assert response.json()["code"] == expected


def test_client_supplied_price_is_never_trusted(
    client_and_version: tuple[TestClient, str],
) -> None:
    client, version = client_and_version
    response = client.post(
        "/api/quotes/calculate",
        json={"catalog_version_id": version, "lines": [{
            "sku": "P-1", "requested_item": REQUEST,
            "price_rub": "0.01",
        }]},
        headers=HEADERS,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_unknown_sku_or_empty_list_rejected(
    client_and_version: tuple[TestClient, str],
) -> None:
    client, version = client_and_version
    for lines, code in (
        ([{"sku": "NO-EXIST", "requested_item": REQUEST}], 422),
        ([], 422),
    ):
        response = client.post(
            "/api/quotes/calculate",
            json={"catalog_version_id": version, "lines": lines},
            headers=HEADERS,
        )
        assert response.status_code == code
