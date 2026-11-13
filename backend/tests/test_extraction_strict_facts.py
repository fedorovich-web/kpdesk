"""Malformed provider facts must fail validation, never become sale quantities."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from kpdesk.extraction.schema import ExtractedRequest
from kpdesk.matching.schema import RequestedItem
from kpdesk.quotes.storage import ManagerSkuSelection


def _payload(**changes: object) -> dict[str, object]:
    return {"lines": [{
        "source_quote": "1 пачка бумаги A4 80 г/м²",
        "description": "Бумага A4", "category": "paper", "quantity": 1,
        "unit": "пачка", "brand": None, "paper_format": "A4", "paper_gsm": 80,
        "pen_ink_color": None, "pen_ink_type": None, "attributes": {}, **changes,
    }]}


@pytest.mark.parametrize("quantity", [True, "1", 1.0])
def test_quantity_requires_an_actual_integer(quantity: object) -> None:
    with pytest.raises(ValidationError):
        ExtractedRequest.model_validate(_payload(quantity=quantity))


@pytest.mark.parametrize("attributes", [None, [], "paper"])
def test_invalid_attributes_raise_validation_error(attributes: object) -> None:
    with pytest.raises(ValidationError):
        ExtractedRequest.model_validate(_payload(attributes=attributes))


@pytest.mark.parametrize("attributes", [None, [], "paper", {1: "A4"}])
def test_matching_attributes_reject_bad_shape_with_validation_error(attributes: object) -> None:
    with pytest.raises(ValidationError):
        RequestedItem.model_validate({
            "description": "Бумага", "category": "paper", "attributes": attributes,
        })


@pytest.mark.parametrize("quantity", [True, "1", 1.0])
def test_matching_quantity_is_not_coerced(quantity: object) -> None:
    with pytest.raises(ValidationError):
        RequestedItem.model_validate({
            "description": "Бумага", "category": "paper", "quantity": quantity,
        })
    with pytest.raises(ValidationError):
        ManagerSkuSelection.model_validate({
            "line_number": 1, "sku": "P-1", "approved_sale_quantity": quantity,
        })
