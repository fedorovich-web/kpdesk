"""A51: only bounded Groq metadata may reach application diagnostic logs."""
from __future__ import annotations

import json
import logging

import httpx
import pytest

from kpdesk.extraction.groq import GroqExtractionError, GroqSettings, extract_request
from kpdesk.extraction.groq_telemetry import (
    GroqUsage,
    parse_usage,
    retry_after_seconds,
    safe_finish_reason,
)

TEXT = "Нужно 20 пачек бумаги А4 80 г/м²"
SECRET = "NEVER-LOG-THE-API-KEY"
MODEL = GroqSettings(test_project_approved=True, api_key=SECRET, model="nvidia/nemotron-3-ultra-550b-a55b:free")
LINE = {
    "source_quote": "20 пачек бумаги А4 80 г/м²",
    "description": "Офисная бумага",
    "category": "paper",
    "quantity": 20, "unit": "пачка",
    "brand": None, "paper_format": "A4", "paper_gsm": 80,
    "pen_ink_color": None, "pen_ink_type": None,
    "attributes": [],
}


def provider_response(
    finish: str = "stop", *, usage: object = None,
) -> httpx.Response:
    return httpx.Response(200, json={
        "model": "nvidia/nemotron-3-ultra-550b-a55b:free", "choices": [{"finish_reason": finish, "message": {
            "content": json.dumps({"lines": [LINE], "instruction_quotes": []}, ensure_ascii=False),
        }}],
        "usage": usage,
    })


@pytest.mark.asyncio
async def test_valid_usage_and_finish_reason_logged_without_customer_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    response = provider_response(usage={
        "prompt_tokens": 321, "completion_tokens": 123, "total_tokens": 444,
        "completion_tokens_details": {"reasoning_tokens": 77},
    })
    with caplog.at_level(logging.INFO, logger="kpdesk.llm"):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: response),
        ) as client:
            extracted = await extract_request(TEXT, MODEL, client)
    assert extracted.lines[0].quantity == 20
    assert len(caplog.records) == 1
    log = caplog.text
    for required in (
        "model=nvidia/nemotron-3-ultra-550b-a55b:free", "outcome=success", "http_status=200",
        "finish_reason=stop", "prompt_tokens=321", "completion_tokens=123",
        "total_tokens=444", "reasoning_tokens=77",
    ):
        assert required in log
    assert TEXT not in log
    assert SECRET not in log
    assert "Офисная бумага" not in log


@pytest.mark.asyncio
async def test_truncated_output_logs_safe_finish_and_usage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    response = provider_response("length", usage={
        "prompt_tokens": 16, "completion_tokens": 30, "total_tokens": 46,
    })
    with caplog.at_level(logging.INFO, logger="kpdesk.llm"):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: response),
        ) as client:
            with pytest.raises(GroqExtractionError) as failed:
                await extract_request(TEXT, MODEL, client)
    assert failed.value.code == "incomplete_output"
    assert "finish_reason=length" in caplog.text
    assert "completion_tokens=30" in caplog.text
    assert "outcome=incomplete_output" in caplog.text
    assert TEXT not in caplog.text


@pytest.mark.asyncio
async def test_rate_limit_retry_after_is_bounded_and_no_body_or_header_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    header = "13"
    body_secret = "PROVIDER-PRIVATE-MESSAGE"
    with caplog.at_level(logging.INFO, logger="kpdesk.llm"):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(
                429, headers={"Retry-After": header},
                json={"error": {"message": body_secret}},
            )),
        ) as client:
            with pytest.raises(GroqExtractionError) as failed:
                await extract_request(TEXT, MODEL, client)
    assert failed.value.code == "rate_limited"
    assert failed.value.retry_after_seconds == 13
    assert "outcome=rate_limited" in caplog.text
    assert "http_status=429" in caplog.text
    assert "retry_after_seconds=13" in caplog.text
    for secret in (TEXT, SECRET, body_secret):
        assert secret not in caplog.text


@pytest.mark.asyncio
async def test_untrusted_finish_reason_and_retry_after_not_echoed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    injected = "INJECTED-LOG-LINE\nSECRET"
    with caplog.at_level(logging.INFO, logger="kpdesk.llm"):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: provider_response(injected)),
        ) as client:
            with pytest.raises(GroqExtractionError) as failed:
                await extract_request(TEXT, MODEL, client)
    assert failed.value.code == "incomplete_output"
    assert "finish_reason=other" in caplog.text
    assert injected not in caplog.text


@pytest.mark.asyncio
async def test_timeout_does_not_log_provider_exception_detail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("SECRET-URL-AND-KEY")
    with caplog.at_level(logging.INFO, logger="kpdesk.llm"):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(broken),
        ) as client:
            with pytest.raises(GroqExtractionError) as failed:
                await extract_request(TEXT, MODEL, client)
    assert failed.value.code == "provider_timeout"
    assert "http_status=None" in caplog.text
    assert "outcome=provider_timeout" in caplog.text
    assert "SECRET-URL-AND-KEY" not in caplog.text


@pytest.mark.parametrize(("raw", "expected"), [
    ({"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}, GroqUsage(3, 2, 5)),
    ({"prompt_tokens": True, "completion_tokens": 2, "total_tokens": 3}, None),
    ({"prompt_tokens": 3, "completion_tokens": -2, "total_tokens": 5}, None),
    ({"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 4}, None),
    ({"prompt_tokens": 3, "completion_tokens": "2", "total_tokens": 5}, None),
    ({"prompt_tokens": 10**12, "completion_tokens": 2, "total_tokens": 10**12 + 2}, None),
    (None, None),
    ([], None),
])
def test_untrusted_provider_usage_is_rejected(raw: object, expected: GroqUsage | None) -> None:
    assert parse_usage(raw) == expected


@pytest.mark.parametrize(("raw", "result"), [
    ("0", 0), ("22", 22), ("99999", 3600),
    ("-1", None), ("25\nFake: log line", None),
    ("99999999999999999999", 3600),
    ("not-an-interval", None), (None, None),
])
def test_retry_after_only_accepts_bounded_seconds(
    raw: str | None, result: int | None,
) -> None:
    assert retry_after_seconds(raw) == result


def test_finish_reason_sanitization() -> None:
    assert safe_finish_reason("stop") == "stop"
    assert safe_finish_reason("length") == "length"
    assert safe_finish_reason("DROP\nrecords") == "other"
    assert safe_finish_reason({"bad": "body"}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("code", "expected"), [
    ("json_validate_failed", "json_validate_failed"),
    ("invalid_request_error", "invalid_request_error"),
    ("PRIVATE-TEXT\nAPI-KEY", "other"),
])
async def test_bad_request_records_only_allowlisted_error_class_without_retry(
    caplog: pytest.LogCaptureFixture, code: str, expected: str,
) -> None:
    calls = 0

    def provider(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": {
            "code": code, "message": SECRET + TEXT, "failed_generation": "PRIVATE-GENERATION",
        }})

    with caplog.at_level(logging.INFO, logger="kpdesk.llm"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            with pytest.raises(GroqExtractionError, match="provider_error"):
                await extract_request(TEXT, MODEL, client)
    assert calls == 1
    assert f"provider_error_code={expected}" in caplog.text
    for private in (SECRET, TEXT, "PRIVATE-GENERATION", "PRIVATE-TEXT"):
        assert private not in caplog.text


# Explicit offline accounting authority; production requires PostgreSQL.
pytestmark = pytest.mark.usefixtures("mock_llm_operation")
