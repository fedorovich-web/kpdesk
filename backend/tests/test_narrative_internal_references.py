"""Internal evidence references never become generated customer-facing prose."""
import json

import httpx
import pytest
from test_commercial_document import content

from kpdesk.extraction.groq import GroqExtractionError, GroqSettings
from kpdesk.quotes.narrative import generate_proposal_narrative, validate_narrative_policy


@pytest.mark.parametrize('leak', [
    'Вариант 1 (product_id: variant:1:line:1) — синий маркер.',
    'Товар variant:2:line:3 предназначен для маркировки.',
    'Проверено по VERIFIED_SPECS.',
    'Параметры из requested_specs.',
    'Сведения confirmed_requested_specs.',
])
def test_generated_text_rejects_internal_evidence_keys_and_ids(leak: str) -> None:
    data = content().narrative.model_dump()
    data['solution'] = leak
    with pytest.raises(ValueError, match='internal_evidence_reference'):
        validate_narrative_policy(json.dumps(data))


def test_customer_text_can_keep_normal_brand_and_variant_labels() -> None:
    data = content().narrative.model_dump()
    data['solution'] = 'Вариант 1: маркер для досок. Вариант 2: маркер для маркировки. Бренд Variant.'
    assert validate_narrative_policy(json.dumps(data)).solution == data['solution']


@pytest.mark.asyncio
async def test_internal_id_leak_stops_before_review_and_any_automatic_retry() -> None:
    data = content().narrative.model_dump()
    data['solution'] = 'Вариант 1 (product_id: variant:1:line:1) — синий маркер.'
    calls = []

    def provider(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"model": "nvidia/nemotron-3-ultra-550b-a55b:free", 'choices': [{'finish_reason': 'stop',
                               'message': {'content': json.dumps(data)}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        with pytest.raises(GroqExtractionError, match='invalid_output'):
            await generate_proposal_narrative('{}', GroqSettings(test_project_approved=True, api_key='offline'), client)
    assert len(calls) == 1


# Explicit offline accounting authority; production requires PostgreSQL.
pytestmark = pytest.mark.usefixtures("mock_llm_operation")
