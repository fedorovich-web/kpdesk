"""A51 regression: untrusted Groq quantities cannot silently set quote quantities."""
from __future__ import annotations

import json

import httpx
import pytest

from kpdesk.extraction.groq import GroqExtractionError, GroqSettings, extract_request
from kpdesk.extraction.grounding import validate_extraction_grounding
from kpdesk.extraction.schema import ExtractedRequest

SETTINGS = GroqSettings(test_project_approved=True, api_key="synthetic-only", model="nvidia/nemotron-3-ultra-550b-a55b:free")


def _extracted(
    quote: str, quantity: int | None, unit: str | None = "пачка",
) -> ExtractedRequest:
    return ExtractedRequest.model_validate({"lines": [{
        "source_quote": quote,
        "description": "Офисная бумага А4",
        "category": "paper",
        "quantity": quantity,
        "unit": unit,
        "brand": None,
        "paper_format": "A4" if "а4" in quote.casefold() else None,
        "paper_gsm": 80 if "80" in quote else None,
        "pen_ink_color": None,
        "pen_ink_type": None,
        "attributes": {},
    }]})


@pytest.mark.parametrize(("fragment", "count", "unit"), [
    ("20 пачек бумаги А4 80 г/м²", 20, "пачка"),
    ("2 коробки бумаги А4", 2, "коробка"),
    ("15 шт. бумаги А4", 15, "штука"),
    ("Нужно две пачки бумаги А4", 2, "пачка"),
    ("10 листов бумаги А4", 10, None),
])
def test_evidence_present_and_never_invented(
    fragment: str, count: int, unit: str | None,
) -> None:
    assert validate_extraction_grounding(
        fragment, _extracted(fragment, count, unit),
    ) == ()


@pytest.mark.parametrize(("fragment", "count", "unit"), [
    ("20 пачек бумаги А4 80 г/м²", 80, "пачка"),
    ("20 пачек бумаги А4", 2, "пачка"),
    ("20 шт. бумаги А4", 20, "пачка"),
    ("Картридж 120XL для принтера А4", 120, "штука"),
    ("Бумага А4 80 г/м²", 80, "пачка"),
    ("Мышь 1600 dpi", 1600, "штука"),
])
def test_ungrounded_quantity_or_unit_is_blocked(
    fragment: str, count: int, unit: str | None,
) -> None:
    assert validate_extraction_grounding(
        fragment, _extracted(fragment, count, unit),
    )


def test_unknown_quantity_remains_reviewable_not_fabricated() -> None:
    quote = "Нужна бумага формата А4"
    assert validate_extraction_grounding(
        quote, _extracted(quote, None, None),
    ) == ()


@pytest.mark.parametrize(("fragment", "count", "unit"), [
    ("1,5 пачки бумаги А4", 5, "пачка"),
    ("1.5 пачки бумаги А4", 5, "пачка"),
    ("-5 пачек бумаги А4", 5, "пачка"),
    ("10–20 пачек бумаги А4", 20, "пачка"),
    ("от 10 до 20 пачек бумаги А4", 20, "пачка"),
    ("20 тыс. пачек бумаги А4", 20, "пачка"),
    ("Бумага А4: 20", 20, "коробка"),
    ("Нужно две пачки бумаги А4", 2, "коробка"),
    ("Нужно двадцать две пачки бумаги А4", 2, "пачка"),
    ("Нужно двадцать две пачки бумаги А4", 20, "пачка"),
    ("Нужно сорок две пачки бумаги А4", 2, "пачка"),
    ("Нужно сто две пачки бумаги А4", 2, "пачка"),
    ("Минус пять пачек бумаги А4", 5, "пачка"),
    ("10 коробок по 2 пачки бумаги А4", 2, "пачка"),
])
def test_partial_numeric_evidence_cannot_authorize_a_sale(
    fragment: str, count: int, unit: str,
) -> None:
    assert validate_extraction_grounding(fragment, _extracted(fragment, count, unit))


@pytest.mark.parametrize(("source", "fragment"), [
    ("Нужно 1,5 пачки бумаги", "5 пачки бумаги"),
    ("Нужно -5 пачек бумаги", "5 пачек бумаги"),
    ("Нужно от 1 до 5 пачек бумаги", "5 пачек бумаги"),
])
def test_cropped_source_quote_cannot_hide_numeric_context(source: str, fragment: str) -> None:
    assert validate_extraction_grounding(source, _extracted(fragment, 5))


def test_fabricated_source_fragment_is_never_echoed_in_reasons() -> None:
    result = validate_extraction_grounding(
        "Заявка без цены и количества",
        _extracted("Секретный товар 999999", 1),
    )
    assert result == ("line_0:source_fragment_missing",)
    assert "Секретный" not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure_kind,event,reason', [
    ('grounding', 'extraction_grounding_rejected', 'line_0:quantity_or_unit_unverified'),
    ('category_attributes', 'extraction_parse_rejected', 'ValueError'),
])
async def test_rejected_extraction_logs_safe_reason_without_payload(
    caplog: pytest.LogCaptureFixture, failure_kind: str, event: str, reason: str,
) -> None:
    source = "20 пачек бумаги А4, плотность 80 г/м²"
    data = _extracted("20 пачек бумаги А4", 80).model_dump(mode="json")
    data['lines'][0]['attributes'] = []
    data['instruction_quotes'] = []
    if failure_kind == 'category_attributes':
        data['lines'][0]['quantity'] = 20
        data['lines'][0]['attributes'] = [{'name':'marker.marker_type','value':'permanent'}]
    calls = []
    def responder(_: httpx.Request) -> httpx.Response:
        calls.append(True)
        return httpx.Response(200, json={"model": "nvidia/nemotron-3-ultra-550b-a55b:free", "choices": [{
            "finish_reason": "stop",
            "message": {"content": json.dumps(data, ensure_ascii=False)},
        }]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        with pytest.raises(GroqExtractionError) as failure:
            await extract_request(source, SETTINGS, client)
    assert failure.value.code == "invalid_output"
    assert len(calls) == 1
    messages = '\n'.join(record.getMessage() for record in caplog.records
                         if record.name == 'kpdesk.extraction')
    assert event in messages and reason in messages
    assert source not in messages and data['lines'][0]['description'] not in messages


# Explicit offline accounting authority; production requires PostgreSQL.
pytestmark = pytest.mark.usefixtures("mock_llm_operation")
