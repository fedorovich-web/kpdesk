"""Offline provenance contracts for real Russian shop public-price-card research."""
from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2] / "ml/data/real/web_catalog_research"


def test_dns_officemag_cards_have_real_source_prices_and_no_false_approvals() -> None:
    data = json.loads(
        (ROOT / "dns_officemag_research_2026-10-09.json").read_text(encoding="utf-8")
    )
    assert data["schema_version"] == "website_product_card_research_v1"
    assert data["currency"] == "RUB"
    assert data["training_authorized"] is False
    assert data["canonical_importable"] is False
    assert data["price_as_of_verified"] is False
    assert data["region_price_verified"] is False
    assert data["independent_gold_labels_count"] == 0
    assert {shop["shop"] for shop in data["shops"]} == {"DNS", "Офисмаг"}
    allowed = {"www.dns-shop.ru", "www.officemag.ru"}
    assert len(data["shops"]) == 2
    total = 0
    for shop in data["shops"]:
        host = urlparse(shop["home_url"]).hostname
        assert host in allowed
        assert urlparse(shop["source_page"]).hostname == host
        assert shop["items"]
        for item in shop["items"]:
            total += 1
            assert item["name"]
            if item.get("source_product_url") is not None:
                assert urlparse(item["source_product_url"]).hostname == host
            prices = (
                item["observed_price_options_rub"]
                if "observed_price_options_rub" in item
                else [item["observed_price_rub"]]
            )
            assert prices
            assert all(
                isinstance(value, str)
                and Decimal(value).is_finite()
                and Decimal(value) > 0
                and re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", value) is not None
                for value in prices
            )
            assert item["sale_unit"] is None
            if len(prices) > 1:
                assert item["price_tier"] == "unresolved_displayed_dual_prices"
                assert item["quantity_price_breaks_unverified"] is True
    assert total == 6
