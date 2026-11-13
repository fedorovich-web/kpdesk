from decimal import Decimal

import pytest

from kpdesk.extraction.analysis import LABELS, analyze_request, product_requirements
from kpdesk.extraction.schema import ExtractedLine, ExtractedRequest
from kpdesk.matching.profiles import CATEGORY_ATTRIBUTES
from kpdesk.matching.schema import CatalogProduct, RequestedItem


def source(**changes: object) -> ExtractedLine:
    values = {"source_quote": "20 коробок бумаги", "description": "Бумага", "category": "paper",
              "quantity": 20, "unit": "коробка", "brand": None, "paper_format": None,
              "paper_gsm": None, "pen_ink_color": None, "pen_ink_type": None, "attributes": {}}
    return ExtractedLine.model_validate({**values, **changes})


def test_live_v8_omissions_require_review_without_inventing_missing_values() -> None:
    request = ExtractedRequest(lines=[
        source(source_quote="Бумага офисная А4 80 г/м², 500 листов — 2 пачки.",
               description="Бумага офисная А4 80 г/м², 500 листов", quantity=2,
               unit="пачка", paper_gsm=80),
        source(source_quote="Ручка шариковая синяя — 10 штук.",
               description="Ручка шариковая синяя", category="pen", quantity=10,
               unit="штука", pen_ink_color="синяя"),
        source(source_quote="Маркер перманентный черный — количество уточняется.",
               description="Маркер перманентный черный", category="marker", quantity=None,
               unit=None, attributes={"marker_type": "перманентный", "ink_color": "черный"}),
    ])
    result = analyze_request(request)
    assert not result.ready_for_matching
    assert [line.missing_fields for line in result.lines] == [
        ("paper_format",), ("pen_ink_type",), ("quantity", "unit"),
    ]
    assert request.lines[0].paper_format is None
    assert request.lines[1].pen_ink_type is None
    assert request.lines[2].quantity is None


def paper() -> CatalogProduct:
    return CatalogProduct(sku="A", name="Бумага A4 80", category="paper", sale_unit="пачка",
                          pieces_per_sale_unit=500, price_rub=Decimal("100.00"),
                          paper_format="A4", paper_gsm=80)


def wanted(**changes: object) -> RequestedItem:
    return RequestedItem.model_validate({
        "description": "Бумага A4 80", "category": "paper", "quantity": 501,
        "unit": "штука", "paper_format": "A4", "paper_gsm": 80, **changes,
    })


def test_missing_details_produce_numbered_questions_linked_to_source() -> None:
    result = analyze_request(ExtractedRequest(lines=[source()]))
    assert not result.ready_for_matching
    assert result.lines[0].source_quote == "20 коробок бумаги"
    assert result.lines[0].missing_fields == ("paper_format", "paper_gsm")
    assert "формат" in result.questions_text.lower()
    assert "плотность" in result.questions_text.lower()
    assert "Позиция 1" in result.questions_text


def test_unknown_category_and_empty_request_do_not_look_ready() -> None:
    assert not analyze_request(ExtractedRequest(lines=[])).ready_for_matching
    line = source(category=None, quantity=None, unit=None)
    result = analyze_request(ExtractedRequest(lines=[line]))
    assert set(result.lines[0].missing_fields) == {"category", "quantity", "unit"}


def test_cartridge_asks_device_or_code_not_product_compatibility_list() -> None:
    line = source(category="toner_cartridge", attributes={"printer_model": "Laser M1",
                                                           "color": "black", "oem_status": "OEM"})
    assert analyze_request(ExtractedRequest(lines=[line])).ready_for_matching
    incomplete = line.model_copy(update={"attributes": {}})
    fields = analyze_request(ExtractedRequest(lines=[incomplete])).lines[0].missing_fields
    assert set(fields) == {"cartridge_identifier", "color", "oem_status"}
    assert "compatible_models" not in fields


def test_optional_brand_is_not_invented_or_required() -> None:
    for brand in (None, "", "   "):
        result = analyze_request(ExtractedRequest(lines=[source(paper_format="A4", paper_gsm=80, brand=brand)]))
        assert result.ready_for_matching and not result.questions_text
        assert "brand" not in result.lines[0].missing_fields


def test_candidate_matrix_shows_exact_mismatch_and_missing_brand() -> None:
    checks = {item.field: item for item in product_requirements(wanted(paper_gsm=120, brand="Acme"), paper())}
    assert checks["paper_gsm"].status == "mismatch"
    assert checks["paper_gsm"].requested == "120" and checks["paper_gsm"].actual == "80"
    assert checks["brand"].status == "unknown_product"


def test_fractional_package_needs_confirmation_and_unknown_conversion_stays_unknown() -> None:
    checks = {item.field: item for item in product_requirements(wanted(), paper())}
    assert checks["quantity_unit"].status == "needs_review"
    approved = {item.field: item for item in product_requirements(wanted(), paper(), 2)}
    assert approved["quantity_unit"].status == "matched"
    assert approved["quantity_unit"].actual == "2 пачки"
    assert "499" in (approved["quantity_unit"].note or "")
    unknown = {item.field: item for item in product_requirements(wanted(unit="коробка"), paper())}
    assert unknown["quantity_unit"].status == "needs_review"
    assert "соотношение" in (unknown["quantity_unit"].note or "")


def test_minimum_constraint_uses_matching_rules_not_string_equality() -> None:
    request = RequestedItem(description="Мышь", category="mouse", quantity=1, unit="штука",
                            attributes={"connection": "USB", "dpi": 1200})
    product = CatalogProduct(sku="M", name="Мышь", category="mouse", sale_unit="штука",
                             pieces_per_sale_unit=1, price_rub=Decimal(200),
                             attributes={"connection": "USB", "dpi": 1600})
    checks = {item.field: item for item in product_requirements(request, product)}
    assert checks["dpi"].status == "matched"
    assert checks["dpi"].actual == "1600"


def test_all_supported_attributes_have_human_labels() -> None:
    assert {rule.name for rules in CATEGORY_ATTRIBUTES.values() for rule in rules} <= LABELS.keys()


def test_customer_questions_do_not_require_internal_scaled_units() -> None:
    line = source(category='mechanical_pencil', description='Механический карандаш')
    before = line.model_dump(mode='json')
    result = analyze_request(ExtractedRequest(lines=[line]))
    assert not result.ready_for_matching
    assert 'lead_diameter_mm_x10' in result.lines[0].missing_fields
    assert 'диаметр грифеля' in result.questions_text.lower()
    assert '(в мм)' in result.questions_text
    assert '×10' not in result.questions_text and 'десятые' not in result.questions_text
    assert line.model_dump(mode='json') == before


@pytest.mark.parametrize('quantity,unit,expected', [
    (1, 'штука', '1 штука'), (2, 'пачка', '2 пачки'), (5, 'коробка', '5 коробок'),
    (11, 'пачка', '11 пачек'), (21, 'пачка', '21 пачка'), (22, 'пачка', '22 пачки'),
])
def test_human_quantity_wording_preserves_confirmed_amounts(
    quantity: int, unit: str, expected: str,
) -> None:
    product = CatalogProduct.model_validate({
        **paper().model_dump(), 'sale_unit': unit,
        'pieces_per_sale_unit': 1 if unit == 'штука' else 500,
    })
    request = wanted(quantity=quantity, unit=unit)
    checks = {item.field: item for item in product_requirements(request, product)}
    assert checks['quantity_unit'].status == 'matched'
    assert checks['quantity_unit'].requested == expected
    assert checks['quantity_unit'].actual == expected
    assert request.quantity == quantity


def contradictory_line(category: str) -> ExtractedLine:
    if category == 'paper':
        return source(description='Самоклеящаяся бумага для этикеток A4 плотность 80',
                      source_quote='Самоклеящаяся бумага для этикеток A4 плотность 80 — 2 пачки',
                      paper_format='A4', paper_gsm=80, quantity=2, unit='пачка')
    return source(description='Стержень для синей шариковой ручки',
                  source_quote='Стержень для синей шариковой ручки — 2 штуки',
                  category='pen', pen_ink_color='blue', pen_ink_type='ballpoint',
                  quantity=2, unit='штука')


@pytest.mark.parametrize('category', ['paper', 'pen'])
def test_analysis_rejects_known_description_category_conflicts_without_erasing_fields(
    category: str,
) -> None:
    request = ExtractedRequest(lines=[contradictory_line(category)])
    before = request.model_dump(mode='json')
    result = analyze_request(request)
    assert not result.ready_for_matching
    assert result.lines[0].missing_fields == ()
    assert result.lines[0].questions
    assert any('категор' in question.lower() for question in result.lines[0].questions)
    assert 'наименование и назначение' in result.questions_text.lower()
    assert 'категор' not in result.questions_text.lower()
    assert request.model_dump(mode='json') == before


@pytest.mark.parametrize('category,description', [
    ('paper', 'Бумага офисная A4 80, не самоклеящиеся этикетки'),
    ('pen', 'Ручка шариковая синяя со стержнем'),
])
def test_analysis_retains_complete_noncontradictory_requests(category: str, description: str) -> None:
    line = contradictory_line(category).model_copy(update={'description': description})
    result = analyze_request(ExtractedRequest(lines=[line]))
    assert result.ready_for_matching
    assert result.lines[0].questions == ()


@pytest.mark.parametrize('category', ['paper', 'pen'])
def test_candidate_requirements_explain_description_conflict(category: str) -> None:
    from kpdesk.extraction.to_matching import to_requested_item

    request = to_requested_item(contradictory_line(category))
    product = paper() if category == 'paper' else CatalogProduct(
        sku='PEN', name='Ручка шариковая синяя', category='pen', sale_unit='штука',
        pieces_per_sale_unit=1, price_rub=Decimal(10),
        pen_ink_color='blue', pen_ink_type='ballpoint',
    )
    checks = {check.field: check for check in product_requirements(request, product)}
    assert checks['description'].status == 'mismatch'
    assert checks['description'].requested == request.description
    assert checks['description'].actual == product.name
    assert checks['description'].note
    assert checks['category'].status == 'matched'


def test_snapshot_freezes_requirements_and_legacy_lines_remain_readable() -> None:
    from kpdesk.quotes.storage import ManagerSkuSelection, _make_snapshot

    line = source(quantity=2, unit="пачка", paper_format="A4", paper_gsm=80)
    snapshot = _make_snapshot(ExtractedRequest(lines=[line]), [paper()],
                              selections=[ManagerSkuSelection(line_number=1, sku="A")])
    encoded = snapshot.model_dump(mode="json")
    checks = {item.field: item for item in snapshot.lines[0].requirements}
    assert checks["paper_gsm"].actual == "80"
    changed = paper().model_copy(update={"paper_gsm": 120})
    assert changed.paper_gsm == 120
    assert {item.field: item.actual for item in snapshot.lines[0].requirements}["paper_gsm"] == "80"
    from kpdesk.quotes.storage import QuoteSnapshot

    assert QuoteSnapshot.model_validate(encoded).lines[0].requirements == snapshot.lines[0].requirements
    del encoded["lines"][0]["requirements"]
    legacy = QuoteSnapshot.model_validate(encoded)
    assert legacy.lines[0].requirements == ()
    assert "requirements" not in legacy.model_dump(mode="json")["lines"][0]


@pytest.mark.parametrize('line', [source(), contradictory_line('paper'), contradictory_line('pen')])
def test_analysis_http_does_not_require_a_catalog_or_provider(line: ExtractedLine) -> None:
    from unittest.mock import Mock, patch
    from uuid import uuid4

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from kpdesk.api.tenant_extraction_routes import router
    from kpdesk.api.tenant_session_routes import get_unreleased_auth_repository
    from kpdesk.catalog.tenant_storage import TenantCatalogRepository

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_unreleased_auth_repository] = lambda: Mock()
    with (
        patch("kpdesk.api.tenant_extraction_routes._required_session", return_value=Mock()),
        patch.object(TenantCatalogRepository, "_authorize") as authorize,
        patch.object(TenantCatalogRepository, "current", side_effect=AssertionError("No catalog read")),
        patch("kpdesk.api.tenant_extraction_routes.extract_request", side_effect=AssertionError("No AI call")),
        TestClient(app) as client,
    ):
        response = client.post(f"/api/session/organizations/{uuid4()}/requests/analyze",
                               json={"extracted_request": {"lines": [line.model_dump(mode="json")]}})
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert not response.json()["ready_for_matching"]
        assert response.json()['lines'][0]['questions']
        authorize.assert_called_once_with("write")
