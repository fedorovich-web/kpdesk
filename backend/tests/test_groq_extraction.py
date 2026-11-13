"""Groq extracts untrusted facts only; prices must never enter authoritative state."""

import json
from collections.abc import AsyncIterator
from typing import cast

import httpx
import pytest

from kpdesk.extraction.groq import (
    GroqExtractionError,
    GroqSettings,
    extract_request,
    structured_inference,
)

TEXT = "Нужно 20 пачек бумаги А4 80 г/м². И ручки синие."
LINE = {
    "source_quote": "20 пачек бумаги А4 80 г/м²",
    "description": "Бумага A4 80 г/м²",
    "category": "paper",
    "quantity": 20,
    "unit": "пачка",
    "brand": None,
    "paper_format": "A4",
    "paper_gsm": 80,
    "pen_ink_color": None,
    "pen_ink_type": None,
}
SETTINGS = GroqSettings(test_project_approved=True, api_key="test-only-not-real", model="nvidia/nemotron-3-ultra-550b-a55b:free")


def mocked_response(lines: list[dict[str, object]], status: int = 200) -> httpx.Response:
    wire_lines = [{**line, "attributes": [
        {'name': str(line['category']) + '.' + name, 'value': value}
        for name, value in cast(dict[str, object], line.get('attributes', {})).items()
        if value is not None
    ]} for line in lines]
    return httpx.Response(
        status,
        json={
            "model": "nvidia/nemotron-3-ultra-550b-a55b:free", "choices": [{
                "finish_reason": "stop",
                "message": {"content": json.dumps({"lines": wire_lines, "instruction_quotes": []}, ensure_ascii=False)},
            }]
        },
    )


@pytest.mark.asyncio
async def test_extract_uses_required_tool_schema_and_keeps_nullable_quantity() -> None:
    sent: list[dict[str, object]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url == "https://openrouter.ai/api/v1/chat/completions"
        assert req.headers["authorization"] == "Bearer test-only-not-real"
        body = json.loads(req.content)
        sent.append(body)
        assert body["model"] == "nvidia/nemotron-3-ultra-550b-a55b:free"
        assert 'response_format' not in body
        assert body['tools'][0]['type'] == 'function'
        assert body['tool_choice']['function']['name'] == body['tools'][0]['function']['name']
        assert body["stream"] is False
        return mocked_response([LINE, {
            **LINE,
            "source_quote": "ручки синие",
            "description": "Синие ручки",
            "category": "pen",
            "quantity": None,
            "unit": None,
            "paper_format": None,
            "paper_gsm": None,
            "pen_ink_color": "синие",
            "pen_ink_type": None,
        }])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await extract_request(TEXT, SETTINGS, client)
    assert len(sent) == 1
    assert len(result.lines) == 2
    assert result.lines[1].quantity is None
    assert result.lines[1].unit is None


@pytest.mark.asyncio
async def test_fabricated_evidence_or_extra_price_causes_validation_error() -> None:
    async def run(line: dict[str, object]) -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: mocked_response([line]))
        ) as client:
            with pytest.raises(GroqExtractionError) as error:
                await extract_request(TEXT, SETTINGS, client)
            assert error.value.code == "invalid_output"

    await run({**LINE, "source_quote": "выдуманные слова"})
    await run({**LINE, "price_rub": 1})


@pytest.mark.asyncio
async def test_provider_429_is_explicit_and_does_not_expose_key() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(429, json={"error": {"message": "token test-only-not-real"}})
        )
    ) as client:
        with pytest.raises(GroqExtractionError) as error:
            await extract_request(TEXT, SETTINGS, client)
    assert error.value.code == "rate_limited"
    assert "test-only-not-real" not in str(error.value)


@pytest.mark.asyncio
async def test_invalid_json_truncated_completion_and_timeout_fail_explicitly() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"model": "nvidia/nemotron-3-ultra-550b-a55b:free", "choices": [{
                "finish_reason": "length", "message": {"content": "{}"},
            }]})
        )
    ) as client:
        with pytest.raises(GroqExtractionError) as error:
            await extract_request(TEXT, SETTINGS, client)
        assert error.value.code == "incomplete_output"

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"model": "nvidia/nemotron-3-ultra-550b-a55b:free", "choices": [{
                "finish_reason": "stop", "message": {"content": "not json"},
            }]})
        )
    ) as client:
        with pytest.raises(GroqExtractionError) as error:
            await extract_request(TEXT, SETTINGS, client)
        assert error.value.code == "invalid_output"

    def timeouts(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("no response")

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeouts)) as client:
        with pytest.raises(GroqExtractionError) as error:
            await extract_request(TEXT, SETTINGS, client)
        assert error.value.code == "provider_timeout"


def test_model_allowlist_and_text_boundary() -> None:
    with pytest.raises(ValueError):
        GroqSettings.model_validate({"test_project_approved": True, "api_key": "test", "model": "untrusted-model"})
    with pytest.raises(ValueError):
        GroqSettings(test_project_approved=True, api_key="", model="nvidia/nemotron-3-ultra-550b-a55b:free")


@pytest.mark.asyncio
async def test_empty_request_rejected_without_network() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: pytest.fail("Network must not be used for invalid input")
        )
    ) as client:
        with pytest.raises(ValueError):
            await extract_request(" ", SETTINGS, client)


class CountingOversizedStream(httpx.AsyncByteStream):
    """The provider must not be allowed to push arbitrary trailing bytes."""
    def __init__(self) -> None:
        self.chunks_seen = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.chunks_seen += 1
        yield b"x" * 1_048_576
        self.chunks_seen += 1
        yield b"y"
        self.chunks_seen += 1
        yield b"this extra provider chunk must never be buffered"


@pytest.mark.asyncio
async def test_groq_response_stops_reading_as_soon_as_size_limit_is_exceeded() -> None:
    response_body = CountingOversizedStream()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=response_body)
        )
    ) as client:
        with pytest.raises(GroqExtractionError) as error:
            await extract_request(TEXT, SETTINGS, client)
    assert error.value.code == "invalid_output"
    assert response_body.chunks_seen == 4


@pytest.mark.asyncio
async def test_groq_response_below_limit_remains_decodable() -> None:
    body = mocked_response([LINE]).content
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=httpx.ByteStream(body))
        )
    ) as client:
        result = await extract_request(TEXT, SETTINGS, client)
    assert len(result.lines) == 1
    assert result.lines[0].source_quote == LINE["source_quote"]


@pytest.mark.asyncio
async def test_provider_error_diagnostics_stop_before_oversized_body() -> None:
    response_body = CountingOversizedStream()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(400, stream=response_body),
    )) as client:
        with pytest.raises(GroqExtractionError, match="provider_error"):
            await extract_request(TEXT, SETTINGS, client)
    assert response_body.chunks_seen == 1


@pytest.mark.asyncio
async def test_valid_empty_object_schema_is_supported_by_local_validation() -> None:
    schema = {'type': 'object', 'properties': {'attributes': {
        'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False,
    }}, 'required': ['attributes'], 'additionalProperties': False}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
        200, json={"model": "nvidia/nemotron-3-ultra-550b-a55b:free", "choices": [{
            "finish_reason": "stop", "message": {"content": '{"attributes":{}}'},
        }]},
    ))) as client:
        result = await structured_inference('fixture', SETTINGS, client, system_prompt='fixture',
            schema_name='valid_empty', schema=schema, parse=json.loads)
    assert result == {'attributes': {}}


@pytest.mark.asyncio
async def test_schema_preflight_distinguishes_field_names_from_schema_keywords() -> None:
    schema: dict[str, object] = {'type': 'object', 'properties': {
        'required': {'type': 'string'},
    }, 'required': ['required'], 'additionalProperties': False}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
        200, json={"model": "nvidia/nemotron-3-ultra-550b-a55b:free", 'choices': [{'finish_reason': 'stop', 'message': {'content': '{"required":"yes"}'}}]},
    ))) as client:
        result = await structured_inference('fixture', SETTINGS, client, system_prompt='fixture',
                                            schema_name='valid_fixture', schema=schema, parse=json.loads)
    assert result == {'required': 'yes'}


# Explicit offline accounting authority; production requires PostgreSQL.
pytestmark = pytest.mark.usefixtures("mock_llm_operation")
