"""Observed fresh writer claims must fail before a paid reviewer is requested."""
import json
from typing import Any
from unittest.mock import Mock

import httpx
import pytest
from test_narrative_review import response, review

from kpdesk.extraction.groq import GroqSettings
from kpdesk.quotes.document import ProposalNarrative
from kpdesk.quotes.narrative import _writer_prompt, generate_proposal_narrative
from kpdesk.quotes.narrative_review import (
    NarrativeReviewRejected,
    _context_issues,
    review_proposal_narrative,
)

FACTS = json.dumps({'selection_origin': 'customer_request', 'products': [
    {'name': 'Бумага офисная A4 80 г/м²', 'verified_specs': {'paper_format': 'A4', 'paper_gsm': '80'}}]})


def draft(solution: str) -> ProposalNarrative:
    return ProposalNarrative(headline='Бумага для печати документов',
        opening='Предлагаем бумагу для стойки приёма.', solution=solution,
        closing='Подтвердите состав или укажите изменения; условия согласуем отдельно.')


@pytest.mark.asyncio
@pytest.mark.parametrize('claim,reason', [
    ('Бумага обеспечивает чёткое изображение текста.', 'unsupported_print_quality'),
    ('Бумага гарантирует чёткую печать.', 'unsupported_print_quality'),
    ('Мы поможем сопоставить выбранный товар с вашей заявкой.', 'supplier_claim_without_company_facts'),
    ('Готовы помочь сопоставить позиции с заявкой.', 'supplier_claim_without_company_facts'),
    ('Бумага сокращает расход материалов.', 'unsupported_consumption_effect'),
])
async def test_local_rejection_never_calls_provider_or_before_request(claim: str, reason: str) -> None:
    transport, before = Mock(), Mock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(NarrativeReviewRejected) as caught:
            await review_proposal_narrative(FACTS, draft(claim), GroqSettings(test_project_approved=True, api_key='offline'),
                                            client, before_request=before)
    assert caught.value.sections_to_revise == ('solution',)
    assert caught.value.can_revise
    assert caught.value.feedback['model_review_performed'] is False
    assert caught.value.feedback['review_origin'] == 'deterministic_context'
    issues = caught.value.feedback['context_issues']
    assert isinstance(issues, list)
    assert any(issue['reason'] == reason for issue in issues)
    transport.assert_not_called()
    before.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('claim,company', [
    ('Бумага — для печати документов.', None),
    ('Бумага не гарантирует чёткую печать.', None),
    ('Поможем сопоставить позиции с вашей заявкой.', {'about': 'Помогаем сопоставлять заявку с каталогом.', 'advantages': ''}),
])
async def test_ordinary_purpose_negation_and_grounded_help_still_require_model_review(claim: str, company: object) -> None:
    facts = json.dumps({**json.loads(FACTS), 'company_facts': company})
    calls = []
    def provider(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(review())
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        await review_proposal_narrative(facts, draft(claim), GroqSettings(test_project_approved=True, api_key='offline'), client)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_local_rejection_corrects_only_bad_section_then_requires_final_model_review() -> None:
    original = draft('Бумага обеспечивает чёткое изображение текста.')
    fixed = 'Бумага A4 плотностью 80 г/м² предназначена для офисной печати.'
    calls, stages = [], []
    def provider(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return response(original.model_dump())
        if len(calls) == 2:
            payload = json.loads(body['messages'][-1]['content'])
            assert payload['editable_sections'] == ['solution']
            assert payload['review']['model_review_performed'] is False
            return response({'solution': fixed})
        assert len(calls) == 3 and body['tools'][0]['function']['name'] == 'kpdesk_proposal_fact_review_v2'
        return response(review())
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        result = await generate_proposal_narrative(FACTS, GroqSettings(test_project_approved=True, api_key='offline'), client,
                                                   before_followup=lambda _, review_stage: stages.append(review_stage))
    assert result == original.model_copy(update={'solution': fixed})
    assert len(calls) == 3 and stages == [False, True]


def test_writer_is_explicit_when_supplier_facts_are_missing() -> None:
    assert 'Сведения о возможностях поставщика отсутствуют' in _writer_prompt(FACTS)


@pytest.mark.asyncio
@pytest.mark.parametrize('technology', ['лазерных принтерах', 'струйных принтерах', 'копировании', 'факсах'])
async def test_paper_format_and_density_do_not_establish_print_technology(technology: str) -> None:
    text = draft('Бумага плотностью 80 г/м² подходит для стандартной печати на ' + technology + '.')
    transport, before = Mock(), Mock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(NarrativeReviewRejected) as caught:
            await review_proposal_narrative(FACTS, text, GroqSettings(test_project_approved=True, api_key='offline'),
                                            client, before_request=before)
    assert caught.value.sections_to_revise == ('solution',)
    assert caught.value.feedback['model_review_performed'] is False
    issues = caught.value.feedback['context_issues']
    assert isinstance(issues, list)
    assert any(issue['reason'] == 'unsupported_print_technology' for issue in issues)
    transport.assert_not_called()
    before.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('evidence,claim', [
    ({'name': 'Бумага A4 для лазерной печати', 'verified_specs': {}}, 'Бумага подходит для лазерных принтеров.'),
    ({'name': 'Бумага A4', 'verified_specs': {'printing_technology': 'inkjet'}}, 'Бумага предназначена для струйной печати.'),
    ({'name': 'Бумага A4', 'verified_specs': {}}, 'Бумага не подходит для лазерных принтеров.'),
    ({'name': 'Бумага A4', 'verified_specs': {}}, 'Бумага предназначена для офисной печати.'),
])
async def test_verified_technology_and_neutral_paper_purpose_still_reach_reviewer(evidence: dict[str, Any], claim: str) -> None:
    facts = json.dumps({'products': [evidence]})
    requests = []
    def provider(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response(review())
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        await review_proposal_narrative(facts, draft(claim), GroqSettings(test_project_approved=True, api_key='offline'), client)
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('extra', [
    {'requested_specs': {'printing_technology': 'laser'}},
    {'verified_specs': {'printing_technology': 'not laser'}},
    {'manager_brief': 'Нужна бумага для лазерного принтера.'},
    {'other_product': {'name': 'Картридж лазерный', 'verified_specs': {}}},
])
async def test_wishes_negated_specs_and_other_products_do_not_verify_paper_technology(extra: dict[str, Any]) -> None:
    product = {'name': 'Бумага A4', 'verified_specs': {}}
    facts = {'products': [product]}
    if 'other_product' in extra:
        facts['products'].append(extra['other_product'])
    elif 'manager_brief' in extra:
        facts.update(extra)
    else:
        product.update(extra)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail('No unsupported review call'))) as client:
        with pytest.raises(NarrativeReviewRejected):
            await review_proposal_narrative(json.dumps(facts), draft('Бумага подходит для лазерных принтеров.'),
                                            GroqSettings(test_project_approved=True, api_key='offline'), client)


def test_print_technology_evidence_does_not_cross_variants_or_papers() -> None:
    unknown = {'name': 'Бумага A4 80 г/м²', 'verified_specs': {}}
    laser = {'name': 'Бумага A4 для лазерной печати', 'verified_specs': {}}
    facts = json.dumps({'products': [unknown], 'alternatives': [
        {'variant_number': 2, 'products': [laser]},
    ]})
    assert _context_issues(facts, draft('Вариант 1: бумага подходит для лазерных принтеров.'))
    assert not _context_issues(facts, draft('Вариант 2: бумага подходит для лазерных принтеров.'))
    assert _context_issues(facts, draft('В обоих вариантах бумага подходит для лазерных принтеров.'))
    assert _context_issues(json.dumps({'products': [unknown, laser]}),
                           draft('Бумага подходит для лазерных принтеров.'))
    named = {'name': 'Бумага LaserOffice A4', 'verified_specs': {'printing_technology': 'laser'}}
    assert not _context_issues(json.dumps({'products': [unknown, named]}),
                               draft('Бумага LaserOffice A4 подходит для лазерных принтеров.'))


@pytest.mark.asyncio
async def test_print_technology_rejection_corrects_only_solution_then_requires_review() -> None:
    original = draft('Бумага подходит для лазерных и струйных принтеров, копирования и факсов.')
    fixed = 'Бумага A4 плотностью 80 г/м² предназначена для офисной печати.'
    calls = []
    def provider(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return response(original.model_dump())
        if len(calls) == 2:
            revision = json.loads(body['messages'][1]['content'])
            assert revision['editable_sections'] == ['solution']
            assert revision['review']['model_review_performed'] is False
            assert revision['review']['context_issues'][0]['reason'] == 'unsupported_print_technology'
            return response({'solution': fixed})
        assert body['tools'][0]['function']['name'] == 'kpdesk_proposal_fact_review_v2'
        return response(review())
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        result = await generate_proposal_narrative(FACTS, GroqSettings(test_project_approved=True, api_key='offline'), client)
    assert result == original.model_copy(update={'solution': fixed})
    assert len(calls) == 3


# Explicit offline accounting authority; production requires PostgreSQL.
pytestmark = pytest.mark.usefixtures("mock_llm_operation")
