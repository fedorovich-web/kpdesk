from __future__ import annotations

import pytest
from pydantic import ValidationError

from kpdesk.extraction.schema import ExtractedRequest
from kpdesk.quotes.storage import LockedQuoteLine, QuoteSnapshot, _digest


def _line() -> LockedQuoteLine:
    return LockedQuoteLine(
        line_number=1, description="Бумага А4", status="needs_review",
        sku=None, product_name=None, requested_quantity=2,
        requested_unit="пачка", sale_quantity=None, sale_unit=None,
        unit_price_rub=None, line_total_rub=None, reason_codes=("no_match",),
    )


def test_legacy_snapshot_without_source_is_readable_with_original_digest() -> None:
    payload = {
        "ready_for_draft": False, "total_rub": None,
        "lines": [_line().model_dump(mode="json")],
    }
    digest = _digest(payload)
    restored = QuoteSnapshot.model_validate(payload)
    assert restored.source_request is None
    assert restored.total_rub is None
    assert _digest(payload) == digest
    assert "source_request" not in payload


def test_source_request_preserves_exact_immutable_typed_input() -> None:
    source = ExtractedRequest.model_validate({"lines": [{
        "source_quote": "2 пачки бумаги А4 80 г/м²",
        "description": "Бумага А4 80",
        "category": "paper", "quantity": 2, "unit": "пачка",
        "brand": None, "paper_format": "A4", "paper_gsm": 80,
        "pen_ink_color": None, "pen_ink_type": None, "attributes": {},
    }]})
    snapshot = QuoteSnapshot(
        ready_for_draft=False, total_rub=None, lines=(_line(),),
        source_request=source,
    )
    payload = snapshot.model_dump(mode="json")
    assert payload["source_request"] == source.model_dump(mode="json")
    assert QuoteSnapshot.model_validate(payload).source_request == source
    assert _digest(payload) != _digest({
        "ready_for_draft": False, "total_rub": None,
        "lines": [_line().model_dump(mode="json")],
    })
    payload["source_request"]["lines"][0]["quantity"] = -1
    with pytest.raises(ValidationError):
        QuoteSnapshot.model_validate(payload)


def test_unknown_snapshot_fields_remain_forbidden() -> None:
    with pytest.raises(ValidationError):
        QuoteSnapshot.model_validate({
            "ready_for_draft": False, "total_rub": 0,
            "lines": [_line().model_dump(mode="json")],
            "untrusted_approval": True,
        })
