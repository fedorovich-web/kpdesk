"""Text requirements must survive extraction and become confirmable quote rules."""
from __future__ import annotations

import json

import httpx
import pytest
from test_groq_extraction import LINE, SETTINGS

from kpdesk.extraction.groq import GroqExtractionError, extract_request
from kpdesk.extraction.schema import ExtractedRequest
from kpdesk.quotes.instructions import propose_instructions


@pytest.mark.asyncio
async def test_budget_and_brand_policy_are_extracted_without_manager_retyping() -> None:
    clauses = ["Бюджет до 30000 рублей", "Исключи бренд Acme"]
    source = "20 пачек бумаги А4 80 г/м². " + ". ".join(clauses)
    payload = {"lines": [{**LINE, "attributes": []}], "instruction_quotes": clauses}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
        200, json={"model":"nvidia/nemotron-3-ultra-550b-a55b:free", "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(payload)}}]},
    ))) as client:
        result = await extract_request(source, SETTINGS, client)
    proposal = propose_instructions("\n".join(result.instruction_quotes))
    assert proposal.can_confirm
    assert str(proposal.rules.budget_max_rub) == "30000"
    assert proposal.rules.excluded_brands == ("acme",)


@pytest.mark.asyncio
async def test_provider_cannot_silently_drop_a_recognizable_budget() -> None:
    source = "20 пачек бумаги А4 80 г/м². Бюджет до 30000 рублей."
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
        200, json={"model":"nvidia/nemotron-3-ultra-550b-a55b:free", "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"lines": [{**LINE, "attributes": []}], "instruction_quotes": []})}}]},
    ))) as client:
        with pytest.raises(GroqExtractionError, match="invalid_output"):
            await extract_request(source, SETTINGS, client)


def test_empty_optional_instruction_field_does_not_change_legacy_request_hash_shape() -> None:
    request = ExtractedRequest.model_validate({"lines": [LINE]})
    assert "instruction_quotes" not in request.model_dump(mode="json")

pytestmark = pytest.mark.usefixtures("mock_llm_operation")
