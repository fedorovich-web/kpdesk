"""Actual inference adapter contract, using an offline provider transport."""
from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest
from test_commercial_document import content
from test_narrative_review import draft_text
from test_quote_pdf import draft

from kpdesk.extraction.groq import GroqExtractionError, GroqSettings
from kpdesk.quotes.narrative import generate_proposal_narrative, proposal_facts


def test_presentation_context_uses_saved_products_without_procurement_prices() -> None:
    document = content()
    payload = proposal_facts(draft(), **document.model_dump(exclude={"issued_on", "narrative"}))
    facts = json.loads(payload)
    assert facts["products"][0]["name"] == "Бумага офисная А4 80 г/м²"
    assert facts["products"][0]["quantity"] == 2
    assert facts["manager_brief"] == document.manager_brief
    assert facts['selection_origin'] == 'manager_selection'
    assert "349.90" not in payload and "1399.60" not in payload
    with pytest.raises(ValueError, match="quote_not_ready"):
        proposal_facts(replace(draft(), ready_for_draft=False), **document.model_dump(
            exclude={"issued_on", "narrative"},
        ))


@pytest.mark.asyncio
async def test_inference_writes_the_offer_from_manager_brief_and_real_products() -> None:
    narrative = content().narrative.model_copy(update={
        'solution': 'В состав предложения включена офисная бумага формата А4 плотностью 80 г/м².',
    })
    seen: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        data = json.loads(request.content)
        if len(seen) == 2:
            assert data['tools'][0]['function']['name'] == "kpdesk_proposal_fact_review_v2"
            assert draft_text(data['messages'][1]['content']) == narrative.model_dump()
            verdict = {name: {'status': 'supported', 'issues': []} for name in type(narrative).model_fields}
            return httpx.Response(200, json={"model": "nvidia/nemotron-3-ultra-550b-a55b:free", 'choices': [{'finish_reason': 'stop',
                'message': {'content': json.dumps(verdict)}}]})
        assert data['tools'][0]['function']['name'] == "kpdesk_commercial_proposal_v2"
        assert "Не придумывай" in data["messages"][0]["content"]
        assert "деловой стиль" in data["messages"][1]["content"]
        return httpx.Response(200, json={"model": "nvidia/nemotron-3-ultra-550b-a55b:free", "choices": [{
            "finish_reason": "stop", "message": {"content": narrative.model_dump_json()},
        }]})

    document = content()
    facts = proposal_facts(draft(), **document.model_dump(exclude={"issued_on", "narrative"}))
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        result = await generate_proposal_narrative(facts, GroqSettings(test_project_approved=True, api_key="offline-only"), client)
    assert result == narrative
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_provider_failure_never_turns_into_template_success() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(503),
    )) as client:
        with pytest.raises(GroqExtractionError, match="provider_error"):
            await generate_proposal_narrative("{}", GroqSettings(test_project_approved=True, api_key="offline-only"), client)


@pytest.mark.asyncio
async def test_model_cannot_supply_financial_totals_in_prose() -> None:
    payload = content().narrative.model_dump()
    payload["opening"] = "Предлагаем всё за 100 рублей."
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
        200, json={"model": "nvidia/nemotron-3-ultra-550b-a55b:free", "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(payload)}}]},
    ))) as client:
        with pytest.raises(GroqExtractionError, match="invalid_output"):
            await generate_proposal_narrative("{}", GroqSettings(test_project_approved=True, api_key="offline-only"), client)


@pytest.mark.asyncio
async def test_live_observed_unsupported_sufficiency_claim_is_rejected() -> None:
    """A list of quantities does not establish how long/for whom supplies suffice."""
    payload = content().narrative.model_dump()
    payload["solution"] = (
        "Две пачки бумаги — этого объёма достаточно для печати и переписки. "
        "Все позиции покрывают потребности 12 рабочих мест."
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
        200, json={"model": "nvidia/nemotron-3-ultra-550b-a55b:free", "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(payload)}}]},
    ))) as client:
        with pytest.raises(GroqExtractionError, match="invalid_output"):
            await generate_proposal_narrative("{}", GroqSettings(test_project_approved=True, api_key="offline-only"), client)


# Explicit offline accounting authority; production requires PostgreSQL.
pytestmark = pytest.mark.usefixtures("mock_llm_operation")
