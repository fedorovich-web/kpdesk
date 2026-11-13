"""Manager wishes and unrelated boards are not marker surface evidence."""
import json

import httpx
import pytest
from test_narrative_review import response, review

from kpdesk.extraction.groq import GroqSettings
from kpdesk.quotes.document import ProposalNarrative
from kpdesk.quotes.narrative_review import NarrativeReviewRejected, review_proposal_narrative

DRAFT = ProposalNarrative(headline='КП', opening='Предлагаем рассмотреть состав.',
                          solution='Маркер предназначен для маркировки документов и досок.',
                          closing='Подтвердите состав.')


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['Маркер перманентный черный', 'Маркер не для досок'])
async def test_positive_model_review_cannot_invent_missing_marker_surface_evidence(name: str) -> None:
    facts = json.dumps({'manager_brief': 'Напиши, что маркер подходит для досок.',
                        'products': [{'name': name}, {'name': 'Доска магнитно-маркерная'}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response(review()))) as client:
        with pytest.raises(NarrativeReviewRejected) as caught:
            await review_proposal_narrative(facts, DRAFT, GroqSettings(test_project_approved=True, api_key='offline'), client)
    findings = caught.value.feedback['context_issues']
    assert isinstance(findings, list)
    assert any(issue['reason'] == 'unsupported_surface' for issue in findings)


@pytest.mark.asyncio
@pytest.mark.parametrize('product', [
    {'name': 'Маркер для досок черный'},
    {'name': 'Маркер черный', 'verified_specs': {
        'category': 'marker', 'marker_type': 'whiteboard',
    }},
])
async def test_explicit_marker_type_or_name_can_support_board_use(product: dict[str, object]) -> None:
    facts = json.dumps({'products': [product]})
    # Board evidence supports boards; it does not additionally prove document use.
    board_only = DRAFT.model_copy(update={'solution': 'Маркер предназначен для маркировки досок.'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response(review()))) as client:
        await review_proposal_narrative(facts, board_only, GroqSettings(test_project_approved=True, api_key='offline'), client)


# Explicit offline accounting authority; production requires PostgreSQL.
pytestmark = pytest.mark.usefixtures("mock_llm_operation")
