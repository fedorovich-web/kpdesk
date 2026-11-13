"""Regression: high fuzzy similarity must not switch explicit product models."""
from __future__ import annotations

from decimal import Decimal

import pytest
from rapidfuzz import fuzz

from kpdesk.matching.schema import CatalogProduct, RequestedItem
from kpdesk.quotes.auto_draft import _explicit_identifiers, build_auto_draft


def _printer(name: str) -> CatalogProduct:
    return CatalogProduct(
        sku="PRN-M404", name=name, category="printer",
        sale_unit="штука", pieces_per_sale_unit=1,
        price_rub=Decimal("2999.00"),
        attributes={
            "print_technology": "laser",
            "color_mode": "mono",
            "max_format": "A4",
        },
    )


def _request(name: str) -> RequestedItem:
    return RequestedItem(
        description=name, category="printer", quantity=1, unit="штука",
        attributes={
            "print_technology": "laser",
            "color_mode": "mono",
            "max_format": "A4",
        },
    )


@pytest.mark.parametrize(
    "requested_name",
    [
        "Принтер лазерный OfficePrint M405 A4",
        "Принтер лазерный OfficePrint M404",
        "Принтер лазерный OfficePrint M404 A4 500",
    ],
)
def test_high_similarity_different_or_missing_model_identifier_requires_review(
    requested_name: str,
) -> None:
    product = _printer("Принтер лазерный OfficePrint M404 A4")
    request = _request(requested_name)
    if "M405" in requested_name:
        assert fuzz.WRatio(request.description, product.name) / 100 > 0.82
    draft = build_auto_draft([request], [product])
    assert draft.ready_for_draft is False
    assert draft.approved is False
    assert draft.total_rub is None
    assert len(draft.lines) == 1
    assert draft.lines[0].status == "needs_review"
    assert draft.lines[0].sku is None
    assert draft.lines[0].reason_codes == ("explicit_identifier_mismatch",)


def test_exact_model_identifiers_preserve_auto_draft_for_compatible_product() -> None:
    product = _printer("Принтер лазерный OfficePrint M404 A4")
    request = _request("Принтер лазерный OfficePrint M404 A4")
    draft = build_auto_draft([request], [product])
    assert draft.ready_for_draft is True
    assert draft.approved is False
    assert draft.total_rub == Decimal("2999.00")
    assert draft.lines[0].status == "auto_selected"
    assert draft.lines[0].sku == "PRN-M404"


def test_latin_and_cyrillic_a4_do_not_trigger_false_identifier_mismatch() -> None:
    assert _explicit_identifiers("Принтер M404 формат А4") == _explicit_identifiers(
        "Принтер M404 формат A4"
    )
    request = _request("Принтер лазерный OfficePrint M404 А4")
    draft = build_auto_draft([request], [_printer(
        "Принтер лазерный OfficePrint M404 A4"
    )])
    assert draft.lines[0].status == "auto_selected"


def test_https_auto_draft_route_refuses_similar_but_different_model() -> None:
    import uuid

    from fastapi.testclient import TestClient
    from test_catalog_api import FakeCatalogRepository

    from kpdesk.api.routes import get_catalog_repository
    from kpdesk.catalog.storage import PublishedCatalog
    from kpdesk.main import create_app

    catalog = PublishedCatalog(
        id=uuid.uuid4(), source_sha256="d" * 64, rows_count=1,
    )
    repository = FakeCatalogRepository(
        published=catalog,
        products=(_printer("Принтер лазерный OfficePrint M404 A4"),),
    )
    app = create_app(testing=True)
    app.dependency_overrides[get_catalog_repository] = lambda: repository
    with TestClient(app) as client:
        def send(description: str) -> dict[str, object]:
            payload = {
                "catalog_version_id": str(catalog.id),
                "extracted_request": {
                    "lines": [{
                        "source_quote": description,
                        "description": description,
                        "category": "printer",
                        "quantity": 1,
                        "unit": "штука",
                        "brand": None,
                        "paper_format": None,
                        "paper_gsm": None,
                        "pen_ink_color": None,
                        "pen_ink_type": None,
                        "attributes": {
                            "print_technology": "laser",
                            "color_mode": "mono",
                            "max_format": "A4",
                        },
                    }],
                },
            }
            response = client.post(
                "/api/quotes/auto-draft",
                json=payload, headers={"Origin": "http://localhost:5173"},
            )
            assert response.status_code == 200, response.text
            result: dict[str, object] = response.json()
            return result

        wrong = send("Принтер лазерный OfficePrint M405 A4")
        assert wrong["ready_for_draft"] is False
        assert wrong["total_rub"] is None
        assert wrong["approval_status"] == "not_approved"
        lines = wrong["lines"]
        assert isinstance(lines, list)
        assert lines[0]["status"] == "needs_review"
        assert lines[0]["sku"] is None
        assert lines[0]["reason_codes"] == ["explicit_identifier_mismatch"]

        matching = send("Принтер лазерный OfficePrint M404 A4")
        assert matching["ready_for_draft"] is True
        assert matching["total_rub"] == "2999.00"
        assert matching["approval_status"] == "not_approved"
