"""Observed false positive reviews must not authorize invented marker media."""
import json
from pathlib import Path

import httpx
import pytest
from test_narrative_review import response, review

from kpdesk.extraction.groq import GroqExtractionError, GroqSettings
from kpdesk.quotes.document import ProposalNarrative
from kpdesk.quotes.narrative import generate_proposal_narrative
from kpdesk.quotes.narrative_review import NarrativeReviewRejected, review_proposal_narrative


@pytest.mark.asyncio
@pytest.mark.parametrize('marker_name', ['Маркер перманентный черный', 'Маркер для досок'])
@pytest.mark.parametrize('claim', [
    'Перманентные маркеры черного цвета предназначены для маркировки документов и поверхностей.',
    'Перманентные маркеры черного цвета – 3 штуки. Предназначены для маркировки документов и других офисных материалов.',
    'Маркеры позволяют быстро маркировать документы и поверхности без риска смывания.',
])
async def test_observed_marker_media_remain_unsupported_after_positive_review(claim: str, marker_name: str) -> None:
    facts = json.dumps({'products': [{'name': marker_name}],
                        'manager_brief': 'Опиши маркировку документов.',
                        'company_facts': {'about': 'Поставляем материалы для документов.'}})
    draft = ProposalNarrative(headline='КП', opening='Предлагаем рассмотреть состав.',
                              solution=claim, closing='Подтвердите состав.')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response(review()))) as client:
        with pytest.raises(NarrativeReviewRejected) as error:
            await review_proposal_narrative(facts, draft, GroqSettings(test_project_approved=True, api_key='offline'), client)
    assert error.value.sections_to_revise == ('solution',)
    assert error.value.can_revise
    assert error.value.feedback['context_issues']


@pytest.mark.asyncio
@pytest.mark.parametrize(('name', 'claim'), [
    ('Маркер перманентный черный', 'Маркеры предназначены для маркировки. Бумага — для печати документов.'),
    ('Маркер перманентный черный', 'Маркеры предназначены для маркировки, а бумага для печати документов.'),
    ('Маркер для документов', 'Маркеры предназначены для маркировки документов.'),
    ('Маркер для гладких поверхностей', 'Маркеры предназначены для маркировки поверхностей.'),
    ('Маркер перманентный черный', 'Маркеры не предназначены для маркировки документов.'),
])
async def test_other_products_explicit_marker_media_and_negations_are_not_invented(
    name: str, claim: str,
) -> None:
    facts = json.dumps({'products': [{'name': name}]})
    draft = ProposalNarrative(headline='КП', opening='Предлагаем рассмотреть состав.',
                              solution=claim, closing='Подтвердите состав.')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response(review()))) as client:
        await review_proposal_narrative(facts, draft, GroqSettings(test_project_approved=True, api_key='offline'), client)


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['baseline', 'revised'])
async def test_saved_live_false_positive_cannot_return_after_one_correction(case: str) -> None:
    fixture = json.loads((Path(__file__).parent / 'fixtures/narrative_marker_media.json').read_text(encoding='utf-8'))[case]
    calls = []

    def provider(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        assert len(calls) <= 3  # One bounded schema-format repair, no unchecked acceptance.
        raw = fixture['outputs'][0 if len(calls) == 1 else 2]
        if len(calls) == 2:
            assert json.loads(body['messages'][-1]['content'])['review']['context_issues']
            assert json.loads(body['messages'][-1]['content'])['review']['model_review_performed'] is False
        return httpx.Response(200, json={"model": "nvidia/nemotron-3-ultra-550b-a55b:free", 'choices': [{'finish_reason': 'stop', 'message': {'content': raw}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        with pytest.raises(GroqExtractionError) as error:
            await generate_proposal_narrative(json.dumps(fixture['facts']), GroqSettings(test_project_approved=True, api_key='offline'), client)
    assert len(calls) == (2 if case == 'baseline' else 3)
    if case == 'baseline':
        assert isinstance(error.value, NarrativeReviewRejected)
        assert error.value.feedback['context_issues']
        assert error.value.sections_to_revise == ('solution',)
    else:
        # The frozen correction rewrites every section. Only solution is now
        # locally flagged, so the unchanged strict patch contract rejects it.
        assert error.value.code == 'invalid_output'


# Explicit offline accounting authority; production requires PostgreSQL.
pytestmark = pytest.mark.usefixtures("mock_llm_operation")
