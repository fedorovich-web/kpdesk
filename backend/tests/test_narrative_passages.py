"""Review findings reference server-owned, lossless text passages."""
import json

import httpx
import pytest
from test_narrative_review import response, review

from kpdesk.extraction.groq import GroqSettings
from kpdesk.quotes.document import ProposalNarrative
from kpdesk.quotes.narrative_review import NarrativeReviewRejected, review_proposal_narrative


@pytest.mark.asyncio
async def test_passage_reference_restores_the_exact_original_including_typos() -> None:
    solution = ('Маркировка, сохраняющуюсь на поверхности.\n' * 30)
    draft = ProposalNarrative(headline='КП', opening='Предлагаем рассмотреть состав.',
                              solution=solution, closing='Подтвердите состав.')
    def provider(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        parts = json.loads(body['messages'][-1]['content'])['draft']
        assert {name: ''.join(part['text'] for part in values) for name, values in parts.items()} == draft.model_dump()
        verdict = review()
        verdict['solution'] = {'status': 'unsupported', 'issues': [
            {'passage_id': 'solution:1', 'reason': 'unsupported_property'},
        ]}
        return response(verdict)
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        with pytest.raises(NarrativeReviewRejected) as caught:
            await review_proposal_narrative('{}', draft, GroqSettings(test_project_approved=True, api_key='offline'), client)
    finding = caught.value.feedback['solution']
    assert isinstance(finding, dict)
    assert finding['issues'][0]['quote'] == solution[400:800]


# Explicit offline accounting authority; production requires PostgreSQL.
pytestmark = pytest.mark.usefixtures("mock_llm_operation")
