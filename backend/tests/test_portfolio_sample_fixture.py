"""Portfolio walkthrough fixture: real parser and incompatible SKU rejection.

All values in this file are explicitly synthetic and not 2026 supplier offers.
"""
from pathlib import Path

from kpdesk.catalog.parse import parse_price_list
from kpdesk.catalog.schema import ImportLimits
from kpdesk.matching.retrieve import retrieve_candidates
from kpdesk.matching.schema import RequestedItem

_SAMPLE = (
    Path(__file__).resolve().parents[2]
    / "frontend/public/templates/kpdesk-portfolio-synthetic-v1.csv"
)


def test_portfolio_csv_uses_the_real_validated_import_contract() -> None:
    raw = _SAMPLE.read_bytes()
    result = parse_price_list(_SAMPLE.name, raw, ImportLimits())
    assert result.publishable, result.errors
    assert len(result.products) == 5
    assert len({product.sku for product in result.products}) == 5
    assert all(product.price_rub > 0 for product in result.products)
    assert {product.category for product in result.products} == {"paper", "pen"}


def test_portfolio_paper_request_excludes_unsafe_near_matches() -> None:
    products = parse_price_list(_SAMPLE.name, _SAMPLE.read_bytes(), ImportLimits()).products
    request = RequestedItem(
        description="Бумага офисная А4 80 г/м2 500 листов",
        category="paper",
        quantity=2,
        unit="пачка",
        paper_format="A4",
        paper_gsm=80,
    )
    results = retrieve_candidates(request, products, limit=5)
    assert [item.product.sku for item in results] == ["PAPER-A4-80"]
    assert all(item.constraints.status == "compatible" for item in results)


def test_portfolio_sample_has_a_genuine_no_match_example() -> None:
    products = parse_price_list(_SAMPLE.name, _SAMPLE.read_bytes(), ImportLimits()).products
    request = RequestedItem(
        description="Бумага офисная А5 120 г/м2",
        category="paper",
        quantity=1,
        unit="пачка",
        paper_format="A5",
        paper_gsm=120,
    )
    assert retrieve_candidates(request, products, limit=5) == []
