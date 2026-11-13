"""Managers preview deterministic candidate matching before any quote approval."""

import uuid
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_catalog_api import FakeCatalogRepository

from kpdesk.api.routes import get_catalog_repository
from kpdesk.catalog.storage import PublishedCatalog
from kpdesk.config import AppSettings
from kpdesk.main import create_app
from kpdesk.matching.schema import CatalogProduct

HEADERS = {"Origin": "http://127.0.0.1:5173"}


def enable_synthetic_ml(client: TestClient) -> None:
    app = client.app
    assert isinstance(app, FastAPI)
    app.state.settings = AppSettings(enable_synthetic_ml=True)

MOUSE = {
    "description": "Беспроводная мышь 1600 DPI", "category": "mouse",
    "attributes": {"connection": "wireless", "dpi": 1600},
    "quantity": 3, "unit": "штука",
}


@pytest.fixture
def client() -> TestClient:
    products = (
        CatalogProduct(
            sku="M-1", name="Мышь беспроводная 1600 DPI", category="mouse",
            sale_unit="штука", pieces_per_sale_unit=1, price_rub=Decimal("496.51"),
            attributes={"connection": "wireless", "dpi": 1600},
        ),
        CatalogProduct(
            sku="M-2", name="Мышь проводная 1600 DPI", category="mouse",
            sale_unit="штука", pieces_per_sale_unit=1, price_rub=Decimal("200.00"),
            attributes={"connection": "wired", "dpi": 1600},
        ),
    )
    repo = FakeCatalogRepository(
        published=PublishedCatalog(
            id=uuid.uuid4(), source_sha256="a" * 64, rows_count=2
        ),
        products=products,
    )
    app = create_app(testing=True)
    app.dependency_overrides[get_catalog_repository] = lambda: repo
    return TestClient(app)


def test_preview_excludes_hard_negative_and_never_auto_approves(client: TestClient) -> None:
    response = client.post(
        "/api/matches/preview",
        json={"requested_item": MOUSE, "mode": "rapidfuzz", "limit": 10},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["catalog_version_id"]
    assert data["mode"] == "rapidfuzz"
    assert data["requires_manager_confirmation"] is True
    assert [p["sku"] for p in data["candidates"]] == ["M-1"]
    assert data["candidates"][0]["status"] == "compatible"
    assert data["candidates"][0]["price_rub"] == "496.51"
    assert data["candidates"][0]["requires_manager_confirmation"] is True


@pytest.mark.parametrize("attributes", [None, [], "paper"])
def test_bad_attributes_are_http_422_instead_of_server_error(
    client: TestClient, attributes: object,
) -> None:
    response = client.post(
        "/api/matches/preview", headers=HEADERS,
        json={"requested_item": {**MOUSE, "attributes": attributes}},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_missing_request_attribute_marks_candidate_for_manual_review(client: TestClient) -> None:
    response = client.post(
        "/api/matches/preview",
        json={"requested_item": {
            "description": "мышь 1600", "category": "mouse",
            "attributes": {"dpi": 1600},
        }, "mode": "tfidf"}, headers=HEADERS,
    )
    assert response.status_code == 200
    assert all(p["status"] == "needs_review" for p in response.json()["candidates"])
    assert all(
        "connection_request_unknown" in p["reason_codes"]
        for p in response.json()["candidates"]
    )


def test_testing_factory_ignores_user_ml_flag_unless_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KP_DESK_ENABLE_SYNTHETIC_ML", "1")
    test_app = create_app(testing=True)
    assert test_app.state.settings.enable_synthetic_ml is False
    assert TestClient(test_app).get("/api/capabilities").json()["model_trained"] is False
    explicitly_enabled = create_app(
        testing=True, settings=AppSettings(enable_synthetic_ml=True),
    )
    assert TestClient(explicitly_enabled).get("/api/capabilities").json()["model_trained"] is True


def test_untrained_ml_never_silently_falls_back(client: TestClient) -> None:
    response = client.post(
        "/api/matches/preview",
        json={"requested_item": MOUSE, "mode": "ml"}, headers=HEADERS,
    )
    assert response.status_code == 503
    assert response.json()["code"] == "model_unavailable"


def test_synthetic_ml_preview_scores_without_auto_approval(client: TestClient) -> None:
    enable_synthetic_ml(client)
    response = client.post(
        "/api/matches/preview",
        json={"requested_item": MOUSE, "mode": "ml"}, headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "ml"
    assert body["requires_manager_confirmation"] is True
    assert [item["sku"] for item in body["candidates"]] == ["M-1"]
    assert body["candidates"][0]["requires_manager_confirmation"] is True
    assert 0 <= body["candidates"][0]["score"] <= 1

    missing = client.post(
        "/api/matches/preview",
        json={"requested_item": {
            "description": "Коврик для мыши", "category": "mouse_pad",
        }, "mode": "ml"}, headers=HEADERS,
    )
    assert missing.status_code == 200, missing.text
    assert missing.json()["candidates"] == []


def test_synthetic_category_suggestion_requires_explicit_acceptance(client: TestClient) -> None:
    disabled = client.post(
        "/api/matches/category-suggestion",
        json={"description": "Коврик для мыши", "original_category": "mouse"},
        headers=HEADERS,
    )
    assert disabled.status_code == 503
    enable_synthetic_ml(client)
    response = client.post(
        "/api/matches/category-suggestion",
        json={"description": "Коврик для мыши", "original_category": "mouse"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    suggestion = response.json()
    assert suggestion["original_category"] == "mouse"
    assert suggestion["requires_explicit_acceptance"] is True
    assert suggestion["best_category"] == "mouse_pad"
    assert suggestion["suggested_category"] in ("mouse_pad", None)
    assert suggestion["abstained"] == (suggestion["suggested_category"] is None)


@pytest.mark.parametrize('description, contradicted_category', [
    ('Стержень для синей шариковой ручки', 'pen'),
    ('1. Стержень для синей шариковой ручки', 'pen'),
    ('Самоклеящаяся бумага для этикеток A4 плотность 80', 'paper'),
    ('Бумага офисная А4 80 г/м2 самоклеящиеся ярлыки', 'paper'),
])
def test_category_suggestion_abstains_on_known_description_contradictions(
    client: TestClient, description: str, contradicted_category: str,
) -> None:
    """A model score must not offer the category rejected by matching constraints."""
    enable_synthetic_ml(client)
    response = client.post(
        '/api/matches/category-suggestion', headers=HEADERS,
        json={'description': description, 'original_category': None},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['suggested_category'] is None
    assert body['abstained'] is True
    # Keep the real model result observable without offering it for application.
    assert body['best_category'] == contradicted_category
    assert body['confidence'] > 0
    assert body['margin'] >= 0.03
    assert body['original_category'] is None
    assert body['requires_explicit_acceptance'] is True


@pytest.mark.parametrize('description, category', [
    ('Ручка шариковая синяя', 'pen'),
    ('Бумага офисная A4 80 г/м²', 'paper'),
])
def test_category_suggestion_retains_unambiguous_ordinary_products(
    client: TestClient, description: str, category: str,
) -> None:
    enable_synthetic_ml(client)
    response = client.post(
        '/api/matches/category-suggestion', headers=HEADERS,
        json={'description': description, 'original_category': None},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['suggested_category'] == category
    assert body['abstained'] is False
    assert body['original_category'] is None


def test_unavailable_pinned_artifact_fails_closed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_synthetic_ml(client)
    def unavailable() -> None:
        raise ValueError("Checksum mismatch")
    monkeypatch.setattr("kpdesk.api.routes.load_local_ml_models", unavailable)
    response = client.post(
        "/api/matches/preview",
        json={"requested_item": MOUSE, "mode": "ml"}, headers=HEADERS,
    )
    assert response.status_code == 503
    assert response.json()["code"] == "model_unavailable"
    assert "Checksum" not in response.text


def test_invalid_mode_or_unknown_attributes_rejected(client: TestClient) -> None:
    for body in (
        {"requested_item": MOUSE, "mode": "madeup"},
        {"requested_item": {**MOUSE, "attributes": {"madeup": "x"}}},
    ):
        response = client.post("/api/matches/preview", json=body, headers=HEADERS)
        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"


def test_no_catalog_preview_fails_clearly() -> None:
    app = create_app(testing=True)
    repo = FakeCatalogRepository()
    app.dependency_overrides[get_catalog_repository] = lambda: repo
    response = TestClient(app).post(
        "/api/matches/preview", json={"requested_item": MOUSE}, headers=HEADERS,
    )
    assert response.status_code == 404
    assert response.json()["code"] == "no_active_catalog"
