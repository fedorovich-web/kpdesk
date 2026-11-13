"""Explicit alpha/OEM/part-number identity must survive fuzzy similarity.

Synthetic examples test fail-safe behavior, not independently verified
manufacturer cross-reference or authorization for replacements.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from rapidfuzz import fuzz

from kpdesk.matching.schema import CatalogProduct, RequestedItem
from kpdesk.quotes.auto_draft import build_auto_draft


def _product(name: str, *, sku: str = "KPD-PRINTER-01") -> CatalogProduct:
    return CatalogProduct(
        sku=sku, name=name, category="printer", sale_unit="штука",
        pieces_per_sale_unit=1, price_rub=Decimal("28500.00"),
        attributes={
            "print_technology": "laser",
            "color_mode": "mono",
            "max_format": "A4",
        },
    )


def _request(description: str) -> RequestedItem:
    return RequestedItem(
        description=description, category="printer", quantity=1,
        unit="штука",
        attributes={
            "print_technology": "laser",
            "color_mode": "mono",
            "max_format": "A4",
        },
    )


@pytest.mark.parametrize("request_name,product_name", [
    ("Принтер OfficePrint модель ALPHA A4", "Принтер OfficePrint модель BETA A4"),
    ("Принтер OfficePrint OEM: ALPHA A4", "Принтер OfficePrint OEM: BETA A4"),
    ("Принтер OfficePrint SKU ALPHA A4", "Принтер OfficePrint SKU BETA A4"),
    ("Принтер OfficePrint P/N: ALPHA A4", "Принтер OfficePrint P/N: BETA A4"),
    ("Принтер OfficePrint артикул ALPHA A4", "Принтер OfficePrint артикул BETA A4"),
])
def test_differing_letter_only_identifiers_never_auto_draft(
    request_name: str, product_name: str,
) -> None:
    assert fuzz.WRatio(request_name, product_name) / 100 >= 0.82
    draft = build_auto_draft([_request(request_name)], [_product(product_name)])
    assert draft.ready_for_draft is False
    assert draft.total_rub is None
    assert draft.lines[0].status == "needs_review"
    assert draft.lines[0].sku is None
    assert draft.lines[0].reason_codes == ("explicit_identifier_mismatch",)


@pytest.mark.parametrize("name", [
    "Принтер OfficePrint модель ALPHA A4",
    "Принтер OfficePrint OEM: ALPHA A4",
    "Принтер OfficePrint SKU: ALPHA A4",
    "Принтер OfficePrint P/N: ALPHA A4",
    "Принтер OfficePrint артикул ALPHA A4",
])
def test_identical_explicit_letter_code_keeps_verified_draft(name: str) -> None:
    draft = build_auto_draft([_request(name)], [_product(name)])
    assert draft.ready_for_draft is True
    assert draft.approved is False
    assert draft.lines[0].status == "auto_selected"
    assert draft.total_rub == Decimal("28500.00")


def test_explicit_supplier_sku_can_match_exact_catalog_sku() -> None:
    item = _product("Принтер OfficePrint лазерный A4", sku="ALPHA")
    draft = build_auto_draft(
        [_request("Принтер OfficePrint лазерный SKU ALPHA A4")], [item],
    )
    assert draft.lines[0].status == "auto_selected"


@pytest.mark.parametrize("marker", ["SKU", "OEM", "P/N", "модель", "артикул"])
def test_explicit_request_code_must_not_match_unrelated_supplier_sku(
    marker: str,
) -> None:
    item = _product("Принтер OfficePrint лазерный A4", sku="BETA")
    draft = build_auto_draft(
        [_request(f"Принтер OfficePrint лазерный {marker} ALPHA A4")], [item],
    )
    assert draft.ready_for_draft is False
    assert draft.lines[0].reason_codes == ("explicit_identifier_mismatch",)


@pytest.mark.parametrize("marker", ["OEM", "P/N", "модель", "артикул"])
def test_manufacturer_code_not_proven_by_coincidentally_equal_supplier_sku(
    marker: str,
) -> None:
    item = _product("Принтер OfficePrint лазерный A4", sku="ALPHA")
    draft = build_auto_draft(
        [_request(f"Принтер OfficePrint лазерный {marker} ALPHA A4")], [item],
    )
    assert draft.ready_for_draft is False
    assert draft.lines[0].sku is None
    assert draft.lines[0].reason_codes == ("explicit_identifier_mismatch",)


def test_product_marked_identifier_missing_from_request_requires_review() -> None:
    item = _product("Принтер OfficePrint OEM ALPHA A4")
    draft = build_auto_draft([_request("Принтер OfficePrint A4")], [item])
    assert draft.lines[0].status == "needs_review"
    assert draft.lines[0].reason_codes == ("explicit_identifier_mismatch",)


def test_bare_words_are_not_treated_as_manufacturer_codes() -> None:
    name = "Принтер лазерный белый корпус, тихая печать"
    draft = build_auto_draft([_request(name)], [_product(name)])
    assert draft.lines[0].status == "auto_selected"


@pytest.mark.parametrize("query,offered", [
    ("Принтер OfficePrint модель ALPHA A4",
     "Принтер OfficePrint model ALPHA A4"),
    ("Принтер OfficePrint арт. ALPHA A4",
     "Принтер OfficePrint артикул ALPHA A4"),
    ("Принтер OfficePrint OEM: ALPHA. A4",
     "Принтер OfficePrint OEM ALPHA A4"),
    ("Принтер OfficePrint part no. ALPHA A4",
     "Принтер OfficePrint MPN ALPHA A4"),
])
def test_equivalent_marker_spellings_preserve_compatible_draft(
    query: str, offered: str,
) -> None:
    result = build_auto_draft([_request(query)], [_product(offered)])
    assert result.ready_for_draft is True
    assert result.lines[0].status == "auto_selected"


def test_part_number_is_not_interchangeable_with_oem_code() -> None:
    request = _request("Принтер OfficePrint P/N ALPHA A4")
    product = _product("Принтер OfficePrint OEM ALPHA A4")
    result = build_auto_draft([request], [product])
    assert result.lines[0].status == "needs_review"
    assert result.lines[0].reason_codes == ("explicit_identifier_mismatch",)


def test_free_text_markers_without_word_boundary_are_not_codes() -> None:
    source = "Принтер OfficePrint с modeling исполнением"
    result = build_auto_draft([_request(source)], [_product(source)])
    assert result.lines[0].status == "auto_selected"


def test_http_auto_draft_does_not_substitute_oem_code() -> None:
    import uuid

    from fastapi.testclient import TestClient
    from test_catalog_api import FakeCatalogRepository

    from kpdesk.api.routes import get_catalog_repository
    from kpdesk.catalog.storage import PublishedCatalog
    from kpdesk.main import create_app

    catalog = PublishedCatalog(
        id=uuid.uuid4(), source_sha256="e" * 64, rows_count=1,
    )
    repository = FakeCatalogRepository(
        published=catalog,
        products=(_product("Принтер OfficePrint OEM ALPHA A4"),),
    )
    app = create_app(testing=True)
    app.dependency_overrides[get_catalog_repository] = lambda: repository

    def payload(description: str) -> dict[str, object]:
        return {
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

    with TestClient(app) as client:
        headers = {"Origin": "http://localhost:5173"}
        mismatch = client.post(
            "/api/quotes/auto-draft",
            json=payload("Принтер OfficePrint OEM BETA A4"),
            headers=headers,
        )
        assert mismatch.status_code == 200, mismatch.text
        data = mismatch.json()
        assert data["ready_for_draft"] is False
        assert data["total_rub"] is None
        assert data["approval_status"] == "not_approved"
        assert data["lines"][0]["status"] == "needs_review"
        assert data["lines"][0]["sku"] is None
        assert data["lines"][0]["reason_codes"] == ["explicit_identifier_mismatch"]

        exact = client.post(
            "/api/quotes/auto-draft",
            json=payload("Принтер OfficePrint OEM ALPHA A4"),
            headers=headers,
        )
        assert exact.status_code == 200, exact.text
        assert exact.json()["ready_for_draft"] is True
        assert exact.json()["approval_status"] == "not_approved"
        assert exact.json()["total_rub"] == "28500.00"
