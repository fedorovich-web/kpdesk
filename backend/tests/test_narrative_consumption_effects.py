"""Recorded live false positive must be rejected without banning ordinary purpose."""
import json

import httpx
import pytest
from test_narrative_review import response, review

from kpdesk.extraction.groq import GroqSettings
from kpdesk.quotes.document import ProposalNarrative
from kpdesk.quotes.narrative_review import NarrativeReviewRejected, review_proposal_narrative


@pytest.mark.asyncio
@pytest.mark.parametrize('claim',[
    'Бумага предназначена для стандартной печати и копирования, обеспечивает чёткое изображение текста и экономичный расход.',
    'Выбранная бумага сокращает расход материалов.',
    'Ручки уменьшают расход чернил.',
])
async def test_model_supported_cannot_authorize_unverified_consumption_effect(claim: str) -> None:
    draft=ProposalNarrative(headline='Офисные материалы',opening='Предлагаем состав по заявке.',
                            solution=claim,closing='Подтвердите состав.')
    facts=json.dumps({'selection_origin':'customer_request','products':[{'name':'Бумага A4 80 г/м²',
        'verified_specs':{'paper_format':'A4','paper_gsm':'80'},'requested_specs':{}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:response(review()))) as client:
        with pytest.raises(NarrativeReviewRejected) as error:
            await review_proposal_narrative(facts,draft,GroqSettings(test_project_approved=True, api_key='offline'),client)
    assert error.value.sections_to_revise==('solution',)
    issues=error.value.feedback['context_issues']
    assert isinstance(issues,list)
    assert any(issue['reason']=='unsupported_consumption_effect' for issue in issues)


@pytest.mark.asyncio
async def test_printing_writing_and_helpful_supplier_explanation_remain_allowed() -> None:
    draft=ProposalNarrative(headline='Материалы для офисных документов',opening='Предлагаем состав по заявке.',
        solution='Бумага — для печати документов, ручки — для записей. Поможем сопоставить позиции с вашей заявкой, чтобы проще согласовать заказ.',
        closing='Подтвердите состав.')
    facts=json.dumps({'selection_origin':'customer_request','products':[],
                      'company_facts':{'about':'Помогаем сопоставлять позиции заявки с каталогом.',
                                       'advantages':''}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:response(review()))) as client:
        await review_proposal_narrative(facts,draft,GroqSettings(test_project_approved=True, api_key='offline'),client)


# Explicit offline accounting authority; production requires PostgreSQL.
pytestmark = pytest.mark.usefixtures("mock_llm_operation")
