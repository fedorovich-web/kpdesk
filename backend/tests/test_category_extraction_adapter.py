"""Compact category-bound provider DTOs preserve the domain/API contract."""
import json

import httpx
import pytest

from kpdesk.extraction.groq import (
    GroqExtractionError,
    GroqSettings,
    _parse_category_request,
    extract_request,
)
from kpdesk.extraction.schema import ExtractedRequest


def wire_line() -> dict[str, object]:
    return {
        'source_quote': 'Бумага А4 80 г/м² — 2 пачки',
        'description': 'Бумага А4 80 г/м²', 'category': 'paper', 'quantity': 2,
        'unit': 'пачка', 'brand': None, 'paper_format': 'A4', 'paper_gsm': 80,
        'pen_ink_color': None, 'pen_ink_type': None, 'attributes': [],
    }


async def run(line: dict[str, object], text: str, *, count: int = 1) -> ExtractedRequest:
    requests: list[dict[str, object]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        prompt = body['messages'][0]['content']
        assert 'marker.ink_color (string)' in prompt
        return httpx.Response(200, json={"model": "nvidia/nemotron-3-ultra-550b-a55b:free", 'choices': [{
            'finish_reason': 'stop',
            'message': {'content': json.dumps({'lines': [line] * count, 'instruction_quotes': []})},
        }]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        result = await extract_request(text, GroqSettings(test_project_approved=True, api_key='offline-only'), client)
    assert len(requests) == 1
    return result


@pytest.mark.asyncio
async def test_twenty_paper_lines_do_not_need_fifty_four_null_fields_each() -> None:
    line = wire_line()
    source = '\n'.join(str(line['source_quote']) for _ in range(20))
    result = await run(line, source, count=20)
    assert len(result.lines) == 20
    assert all(row.attributes == {} and row.quantity == 2 for row in result.lines)


@pytest.mark.asyncio
async def test_category_typed_attributes_keep_existing_domain_contract() -> None:
    text = 'Картридж черный совместимый HP 12A для HP LaserJet 1010, количество 2 штуки'
    line = {**wire_line(), 'source_quote': text, 'description': 'Картридж HP 12A',
            'category': 'toner_cartridge', 'unit': 'штука', 'paper_format': None, 'paper_gsm': None,
            'attributes': [{'name': 'toner_cartridge.cartridge_code', 'value': '12A'},
                           {'name': 'toner_cartridge.compatible_models', 'value': ['HP LaserJet 1010']},
                           {'name': 'toner_cartridge.color', 'value': 'черный'},
                           {'name': 'toner_cartridge.oem_status', 'value': 'compatible'}]}
    result = await run(line, text)
    assert result.lines[0].attributes == {
        'cartridge_code': '12A', 'color': 'черный',
        'compatible_models': ['HP LaserJet 1010'], 'oem_status': 'compatible',
    }


@pytest.mark.asyncio
@pytest.mark.parametrize('attributes', [
    {}, [{'name': 'paper.sheets', 'value': 80}], [{'name': 'paper.sheets', 'value': None}],
    [{'name': 'unknown_attribute', 'value': 'x'}], [{'name': 'price_rub', 'value': 1}],
    [{'name': 'marker.ink_color', 'value': 'черный'}],
])
async def test_unknown_or_irrelevant_properties_fail_even_when_null(attributes: object) -> None:
    line = {**wire_line(), 'attributes': attributes}
    with pytest.raises(GroqExtractionError, match='invalid_output'):
        await run(line, str(line['source_quote']))


@pytest.mark.asyncio
async def test_category_transport_does_not_bypass_source_grounding() -> None:
    line = {**wire_line(), 'source_quote': 'Бумага А4 120 г/м² — 2 пачки'}
    with pytest.raises(GroqExtractionError, match='invalid_output'):
        await run(line, 'Бумага А4 80 г/м² — 2 пачки')


def test_duplicate_json_properties_cannot_silently_replace_facts() -> None:
    raw = json.dumps({'lines': [wire_line()], 'instruction_quotes': []})
    for altered in (raw.replace('"quantity": 2', '"quantity": 2, "quantity": 20'),
                    raw.replace('"attributes": []', '"attributes": [{"name": "x", "name": "y", "value": 1}]')):
        with pytest.raises(ValueError, match='duplicate_json_property'):
            _parse_category_request(altered)


def test_repeated_pairs_cannot_overwrite_a_confirmed_attribute() -> None:
    row = {**wire_line(), 'category': 'marker', 'paper_format': None, 'paper_gsm': None,
           'attributes': [{'name': 'marker.ink_color', 'value': 'черный'},
                          {'name': 'marker.ink_color', 'value': 'красный'}]}
    with pytest.raises(ValueError, match='duplicate_or_unknown_attribute_value'):
        _parse_category_request(json.dumps({'lines': [row], 'instruction_quotes': []}))


# Explicit offline accounting authority; production requires PostgreSQL.
pytestmark = pytest.mark.usefixtures("mock_llm_operation")
