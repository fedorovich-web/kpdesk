"""Known source/unit contradictions must not depend solely on an LLM verdict."""
import json

import httpx
import pytest
from test_narrative_review import response, review

from kpdesk.extraction.groq import GroqSettings
from kpdesk.quotes.document import ProposalNarrative
from kpdesk.quotes.narrative_review import NarrativeReviewRejected, review_proposal_narrative


@pytest.mark.asyncio
@pytest.mark.parametrize('claim', [
    'Для повседневной работы 12 сотрудников.',
    'В запросе указаны бумага и ручки.',
    'Все позиции соответствуют заявленным характеристикам.',
])
async def test_false_positive_review_cannot_waive_known_context_contradictions(claim: str) -> None:
    facts = json.dumps({'manager_brief': 'Офис на 12 рабочих мест.', 'selection_origin': 'manager_selection'})
    draft = ProposalNarrative(headline='КП', opening=claim, solution='Предлагаем рассмотреть состав.',
                              closing='Подтвердите состав.')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response(review()))) as client:
        with pytest.raises(NarrativeReviewRejected, match='unverified_narrative') as error:
            await review_proposal_narrative(facts, draft, GroqSettings(test_project_approved=True, api_key='offline'), client)
    assert 'context_issues' in error.value.feedback


@pytest.mark.asyncio
async def test_explicit_headcount_and_actual_request_are_not_workplace_substitution() -> None:
    facts = json.dumps({'manager_brief': 'Офис на 12 рабочих мест для 12 сотрудников.',
                        'selection_origin': 'customer_request'})
    draft = ProposalNarrative(headline='КП', opening='Для работы 12 сотрудников.',
                              solution='В запросе указаны бумага и ручки.', closing='Подтвердите состав.')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response(review()))) as client:
        await review_proposal_narrative(facts, draft, GroqSettings(test_project_approved=True, api_key='offline'), client)


# Explicit offline accounting authority; production requires PostgreSQL.
pytestmark = pytest.mark.usefixtures("mock_llm_operation")
