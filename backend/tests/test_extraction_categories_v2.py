"""Groq v2 extracts category-specific facts; matching always requires manager review."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import cast

import httpx
import pytest
from pydantic import ValidationError

from kpdesk.extraction.groq import GroqSettings, extract_request
from kpdesk.extraction.schema import ExtractedLine
from kpdesk.extraction.to_matching import ExtractionNeedsReview, to_requested_item
from kpdesk.matching.constraints import check_constraints
from kpdesk.matching.schema import CatalogProduct


@pytest.fixture
def mouse_line() -> dict[str, object]:
    return {
        "source_quote": "две беспроводные мыши 1600 dpi",
        "description": "Мышь беспроводная 1600 dpi",
        "category": "mouse",
        "quantity": 2,
        "unit": "штука",
        "brand": None,
        "paper_format": None,
        "paper_gsm": None,
        "pen_ink_color": None,
        "pen_ink_type": None,
        "attributes": {
            "connection": "wireless", "dpi": 1600,
        },
    }


def test_v2_typed_mouse_extraction_maps_to_matching_without_prices(
    mouse_line: dict[str, object],
) -> None:
    extracted = ExtractedLine.model_validate(mouse_line)
    request = to_requested_item(extracted)
    assert request.category == "mouse"
    assert request.quantity == 2
    assert request.attributes == {"connection": "wireless", "dpi": 1600}
    matching = CatalogProduct(
        sku="513519", name="Мышь SONNEN беспроводная 1600 dpi",
        category="mouse", sale_unit="штука", pieces_per_sale_unit=1,
        price_rub=Decimal("496.51"), attributes={"connection": "wireless", "dpi": 1600},
    )
    assert check_constraints(request, matching).status == "compatible"


def test_incomplete_category_must_be_manually_classified(
    mouse_line: dict[str, object],
) -> None:
    mouse_line["category"] = None
    mouse_line["attributes"] = {}
    with pytest.raises(ExtractionNeedsReview, match="category"):
        to_requested_item(ExtractedLine.model_validate(mouse_line))


@pytest.mark.parametrize("attributes", [
    {"connection": "wireless", "dpi": True},
    {"connection": "wireless", "dpi": "1600"},
    {"connection": "wireless", "dpi": -1},
    {"connection": "wireless", "dpi": 1600, "is_admin": 1},
])
def test_malformed_or_unrecognized_extracted_attributes_rejected(
    mouse_line: dict[str, object], attributes: dict[str, object],
) -> None:
    mouse_line["attributes"] = attributes
    with pytest.raises(ValidationError):
        ExtractedLine.model_validate(mouse_line)


def test_irrelevant_attributes_cannot_be_silently_ignored(
    mouse_line: dict[str, object],
) -> None:
    mouse_line["category"] = "mouse"
    mouse_line["attributes"] = {"connection": "wireless", "dpi": 1600, "spine_mm": 70}
    with pytest.raises(ValidationError, match="spine_mm"):
        ExtractedLine.model_validate(mouse_line)


def test_missing_critical_mouse_attribute_yields_needs_review(
    mouse_line: dict[str, object],
) -> None:
    mouse_line["attributes"] = {"dpi": 1600}
    request = to_requested_item(ExtractedLine.model_validate(mouse_line))
    item = CatalogProduct(
        sku="M-1", name="Мышь беспроводная 1600",
        category="mouse", sale_unit="штука", pieces_per_sale_unit=1,
        price_rub=Decimal("500.00"), attributes={"connection": "wireless", "dpi": 1600},
    )
    result = check_constraints(request, item)
    assert result.status == "needs_review"
    assert "connection_request_unknown" in result.reason_codes


@pytest.mark.asyncio
async def test_groq_v2_strict_schema_and_inference_returns_new_category(
    mouse_line: dict[str, object],
) -> None:
    # A countable noun is not proof of a commercial sale unit. This test
    # exercises category/schema extraction with explicit piece-unit evidence.
    source = "Пожалуйста, дайте беспроводные мыши 1600 dpi: две штуки."
    mouse_line["source_quote"] = "беспроводные мыши 1600 dpi: две штуки"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        schema = payload["tools"][0]["function"]["parameters"]
        inner = schema["properties"]["lines"]["items"]
        assert "attributes" in inner["properties"]
        assert all(item["additionalProperties"] is False for item in inner["properties"]["attributes"]["items"]["anyOf"])
        assert set(inner["required"]) == set(inner["properties"])
        assert payload["tools"][0]["type"] == "function"
        return httpx.Response(200, json={"model": "nvidia/nemotron-3-ultra-550b-a55b:free", "choices": [{
            "finish_reason": "stop",
            "message": {"content": json.dumps({"lines": [{**mouse_line, 'attributes': [
                {'name': 'mouse.' + name, 'value': value}
                for name, value in cast(dict[str, object], mouse_line['attributes']).items()
            ]}], "instruction_quotes": []}, ensure_ascii=False)},
        }]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await extract_request(
            source, GroqSettings(test_project_approved=True, api_key="test-secret"), client,
        )
    assert to_requested_item(result.lines[0]).category == "mouse"


def test_groq_strict_schema_nullable_other_categories_do_not_break_valid_item(
    mouse_line: dict[str, object],
) -> None:
    """Previously saved broad nullable domain snapshots remain readable."""
    from kpdesk.matching.profiles import CATEGORY_ATTRIBUTES

    names = {r.name for rules in CATEGORY_ATTRIBUTES.values() for r in rules}
    mouse_line["attributes"] = {
        name: ({"connection": "wireless", "dpi": 1600}.get(name))
        for name in names
    }
    line = ExtractedLine.model_validate(mouse_line)
    assert to_requested_item(line).attributes == {"connection": "wireless", "dpi": 1600}


def test_broad_categories_appear_in_strict_groq_schema() -> None:
    from typing import Any

    from kpdesk.extraction.groq import _structured_schema

    schema = cast(dict[str, Any], _structured_schema())
    fields = schema["properties"]["lines"]["items"]["properties"]
    categories = fields["category"]["enum"]
    for code in ("pencil", "document_folder", "marker", "notebook",
                 "toner_cartridge", "printer", "stationery_other"):
        assert code in categories
    compatible = next(item for item in fields['attributes']['items']['anyOf']
                      if 'toner_cartridge.compatible_models' in item['properties']['name']['enum'])
    assert compatible['properties']['value'] == {'type': 'array', 'items': {'type': 'string'}}


# Explicit offline accounting authority; production requires PostgreSQL.
pytestmark = pytest.mark.usefixtures("mock_llm_operation")
