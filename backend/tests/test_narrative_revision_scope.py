"""A correction cannot replace previously accepted sections of the proposal."""
import json

import httpx
import pytest
from test_narrative_correction import FIXED, negative
from test_narrative_review import FACTS, TEXT, response, review

from kpdesk.extraction.groq import GroqExtractionError, GroqSettings
from kpdesk.quotes.narrative import generate_proposal_narrative


@pytest.mark.asyncio
async def test_revision_only_sends_and_accepts_flagged_sections() -> None:
    calls = []
    def provider(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        calls.append(data)
        if len(calls) == 3:
            schema = data['tools'][0]['function']['parameters']
            assert set(schema['properties']) == {'solution'}
            assert schema['required'] == ['solution']
            assert schema['additionalProperties'] is False
            assert json.loads(data['messages'][-1]['content'])['editable_sections'] == ['solution']
        payloads = [TEXT.model_dump(), negative(), {'solution': FIXED.solution}, review()]
        return response(payloads[len(calls)-1])
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        result = await generate_proposal_narrative(FACTS, GroqSettings(test_project_approved=True, api_key='offline'), client)
    assert result == FIXED
    assert result.headline == TEXT.headline and result.opening == TEXT.opening and result.closing == TEXT.closing
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_extra_revision_field_is_rejected_even_when_a_model_would_approve_it() -> None:
    calls = []
    def provider(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        payloads = [TEXT.model_dump(), negative(),
                    {**FIXED.model_dump(), 'headline': 'Подмена проверенного заголовка'}, review()]
        return response(payloads[len(calls)-1])
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        with pytest.raises(GroqExtractionError, match='invalid_output'):
            await generate_proposal_narrative(FACTS, GroqSettings(test_project_approved=True, api_key='offline'), client)
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_context_guard_marks_only_bad_section_editable_without_model_vote() -> None:
    facts = json.dumps({'manager_brief': 'Офис на 12 рабочих мест.', 'selection_origin': 'manager_selection'})
    initial = FIXED.model_copy(update={'opening': 'Для работы 12 сотрудников.'})
    opening = 'Предлагаем рассмотреть состав для офиса на 12 рабочих мест.'
    calls = []
    def provider(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 2:
            revision = json.loads(body['messages'][-1]['content'])
            assert revision['editable_sections'] == ['opening']
            assert revision['review']['context_issues'][0]['reason'] == 'unsupported_headcount'
        return response([initial.model_dump(), {'opening': opening}, review()][len(calls)-1])
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        result = await generate_proposal_narrative(facts, GroqSettings(test_project_approved=True, api_key='offline'), client)
    assert result == initial.model_copy(update={'opening': opening})
    assert len(calls) == 3


# Explicit offline accounting authority; production requires PostgreSQL.
pytestmark = pytest.mark.usefixtures("mock_llm_operation")
