"""Groq HTTP endpoint: explicit errors and no insecure fallback."""

import json
from collections.abc import AsyncIterator
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kpdesk.api.routes import get_groq_client, get_groq_settings
from kpdesk.extraction.groq import GroqSettings
from kpdesk.main import create_app

SOURCE = "Нужно 20 пачек бумаги А4 80 г/м²"
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
    "attributes": [],
}
HEADERS = {"Origin": "http://localhost:5173"}


def response_json(lines: list[dict[str, object]]) -> httpx.Response:
    return httpx.Response(200, json={
        "model": "nvidia/nemotron-3-ultra-550b-a55b:free", "choices": [{
            "finish_reason": "stop",
            "message": {"content": json.dumps({"lines": lines, "instruction_quotes": []}, ensure_ascii=False)},
        }]
    })


def make_client(handler: httpx.MockTransport) -> TestClient:
    app = create_app(testing=True)
    app.dependency_overrides[get_groq_settings] = lambda: GroqSettings(test_project_approved=True, 
        api_key="never-expose-in-tests", model="nvidia/nemotron-3-ultra-550b-a55b:free",
    )

    async def mock_client() -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(transport=handler) as c:
            yield c

    app.dependency_overrides[get_groq_client] = mock_client
    return TestClient(app)


def test_legacy_anonymous_endpoint_cannot_bypass_shared_budget() -> None:
    client = make_client(httpx.MockTransport(lambda _: pytest.fail('No unauthenticated dispatch')))
    response = client.post('/api/requests/extract', json={'text': SOURCE}, headers=HEADERS)
    assert response.status_code == 503
    assert response.json()['code'] == 'authenticated_provider_required'
    assert 'never-expose-in-tests' not in response.text


@pytest.mark.parametrize(
    ("model", "text"),
    [
        ("not-a-model", SOURCE),
        ("nvidia/nemotron-3-ultra-550b-a55b:free", ""),
        ("nvidia/nemotron-3-ultra-550b-a55b:free", "a" * 12001),
    ],
)
def test_invalid_extract_input_is_rejected_before_network(model: str, text: str) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        pytest.fail("Must not send invalid request to Groq")

    client = make_client(httpx.MockTransport(handler))
    result = client.post(
        "/api/requests/extract", json={"text": text, "model": model}, headers=HEADERS,
    )
    assert result.status_code == 422
    assert result.json()["code"] == "validation_error"


def test_not_configured_returns_explicit_503() -> None:
    client = make_client(httpx.MockTransport(lambda _: pytest.fail("Never call provider")))
    app = cast(FastAPI, client.app)
    app.dependency_overrides.pop(get_groq_settings)
    # Mask local credentials regardless of whether the user has configured the real key.
    def missing_settings() -> GroqSettings | None:
        return None
    app.dependency_overrides[get_groq_settings] = missing_settings
    result = client.post("/api/requests/extract", json={"text": SOURCE}, headers=HEADERS)
    assert result.status_code == 503
    assert result.json()["code"] == "provider_not_configured"


