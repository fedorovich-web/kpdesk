"""Requested properties are not evidence about selected products."""
import json
from dataclasses import replace
from typing import Literal

import httpx
import pytest
from test_document_alternatives import saved
from test_narrative_review import response, review
from test_quote_pdf import draft

from kpdesk.extraction.analysis import RequirementCheck
from kpdesk.extraction.groq import GroqSettings
from kpdesk.extraction.schema import ExtractedLine, ExtractedRequest
from kpdesk.quotes.alternatives import available_alternatives
from kpdesk.quotes.document import ProposalNarrative
from kpdesk.quotes.narrative import proposal_facts
from kpdesk.quotes.narrative_review import NarrativeReviewRejected, review_proposal_narrative
from kpdesk.quotes.storage import StoredQuote


def marker_quote() -> StoredQuote:
    source = draft()
    line = source.lines[0].model_copy(update={'product_name': 'Маркер перманентный черный',
                                             'line_number': 1, 'description': 'Маркер для досок'})
    requested = ExtractedLine(source_quote='Маркер для досок', description='Маркер для досок',
                              category='marker', quantity=2, unit='пачка', brand=None,
                              paper_format=None, paper_gsm=None, pen_ink_color=None, pen_ink_type=None,
                              attributes={'marker_type': 'whiteboard'})
    return replace(source, lines=(line,), source_request=ExtractedRequest(lines=[requested]))


def facts(quote: StoredQuote) -> str:
    return proposal_facts(quote, issuer_name='Учебный поставщик', recipient_name='Учебный клиент',
                          manager_brief='Предложи выбранный состав.', commercial_terms='')


def test_request_is_preserved_as_request_without_promoting_it_to_selected_properties() -> None:
    product = json.loads(facts(marker_quote()))['products'][0]
    assert product['product_id'] == 'variant:1:line:1'
    assert product['requested_specs']['attributes']['marker_type'] == 'whiteboard'
    assert product['verified_specs'] == {}
    assert 'confirmed_requested_specs' not in product


@pytest.mark.parametrize('status', ['provided', 'mismatch', 'missing_request', 'unknown_product', 'needs_review'])
def test_unmatched_checks_do_not_promote_requested_or_actual_to_verified(status: str) -> None:
    quote = marker_quote()
    check = RequirementCheck.model_validate({'field': 'marker_type', 'label': 'Тип маркера',
                                             'requested': 'whiteboard', 'actual': 'permanent', 'status': status})
    quote = replace(quote, lines=(quote.lines[0].model_copy(update={'requirements': (check,)}),))
    assert json.loads(facts(quote))['products'][0]['verified_specs'] == {}


@pytest.mark.parametrize('actual', [None, '', '  '])
def test_matched_without_actual_cannot_be_used_as_property_evidence(actual: str | None) -> None:
    quote = marker_quote()
    check = RequirementCheck(field='marker_type', label='Тип маркера', requested='whiteboard',
                              actual=actual, status='matched')
    quote = replace(quote, lines=(quote.lines[0].model_copy(update={'requirements': (check,)}),))
    assert json.loads(facts(quote))['products'][0]['verified_specs'] == {}


def test_verified_uses_actual_from_own_check_instead_of_requested_spelling() -> None:
    quote = marker_quote()
    checks = (RequirementCheck(field='marker_type', label='Тип маркера', requested='для досок',
                               actual='whiteboard', status='matched'),
              RequirementCheck(field='quantity_unit', label='Количество', requested='2 пачки',
                               actual='2 пачки', status='matched'))
    quote = replace(quote, lines=(quote.lines[0].model_copy(update={'requirements': checks}),))
    assert json.loads(facts(quote))['products'][0]['verified_specs'] == {'marker_type': 'whiteboard'}


def test_alternative_ids_and_evidence_are_distinct_and_derived_from_own_lines() -> None:
    quote, catalog = saved()
    # Both satisfy HB; actual catalogue spelling deliberately differs by variant.
    catalog[1] = catalog[1].model_copy(update={'attributes': {**catalog[1].attributes, 'lead_grade': 'hb'}})
    alternatives = available_alternatives(quote, catalog)
    payload = json.loads(proposal_facts(quote, issuer_name='Поставщик', recipient_name='',
                                       manager_brief='Предложи варианты.', commercial_terms='',
                                       alternatives=alternatives))
    primary = payload['products'][0]
    alternative = payload['alternatives'][0]['products'][0]
    assert primary['product_id'] == 'variant:1:line:1'
    assert alternative['product_id'] == 'variant:2:line:1'
    assert primary['verified_specs']['lead_grade'] == 'HB'
    assert alternative['verified_specs']['lead_grade'] == 'hb'
    assert primary['verified_specs'] == {c.field: c.actual for c in quote.lines[0].requirements
                                         if c.status == 'matched' and c.actual and c.field != 'quantity_unit'}
    assert alternative['verified_specs'] == {c.field: c.actual for c in alternatives[0].lines[0].requirements
                                             if c.status == 'matched' and c.actual and c.field != 'quantity_unit'}


def test_reordered_lines_keep_their_own_request_and_actual_values() -> None:
    quote = marker_quote()
    assert quote.source_request is not None
    requested = quote.source_request.lines[0]
    checks = tuple(RequirementCheck(field='marker_type', label='Тип маркера', requested=kind,
                                    actual=kind, status='matched') for kind in ('whiteboard', 'permanent'))
    first = quote.lines[0].model_copy(update={'product_name': 'Маркер синий', 'requirements': (checks[0],)})
    second = first.model_copy(update={'product_name': 'Маркер черный', 'line_number': 2, 'requirements': (checks[1],)})
    quote = replace(quote, lines=(second, first), source_request=ExtractedRequest(lines=[
        requested, requested.model_copy(update={'attributes': {'marker_type': 'permanent'}}),
    ]))
    products = json.loads(facts(quote))['products']
    assert [p['product_id'] for p in products] == ['variant:1:line:2', 'variant:1:line:1']
    for product, kind in zip(products, ('permanent', 'whiteboard'), strict=True):
        assert product['requested_specs']['attributes']['marker_type'] == kind
        assert product['verified_specs']['marker_type'] == kind


@pytest.mark.parametrize('number', [0, -1, 2, 21])
def test_invalid_request_line_reference_fails_before_linking_wrong_evidence(number: int) -> None:
    quote = marker_quote()
    quote = replace(quote, lines=(quote.lines[0].model_copy(update={'line_number': number}),))
    with pytest.raises(ValueError, match='proposal_line_context_mismatch'):
        facts(quote)


def test_duplicate_ids_cannot_share_one_request_line() -> None:
    quote = marker_quote()
    quote = replace(quote, lines=(quote.lines[0], quote.lines[0]))
    with pytest.raises(ValueError, match='proposal_line_context_mismatch'):
        facts(quote)


@pytest.mark.asyncio
@pytest.mark.parametrize('source_key', ['requested_specs', 'confirmed_requested_specs'])
async def test_customer_wish_cannot_override_missing_product_evidence_after_positive_review(source_key: str) -> None:
    data = {'products': [{'name': 'Маркер перманентный черный', 'product_id': 'variant:1:line:1',
                          source_key: {'category': 'marker', 'attributes': {'marker_type': 'whiteboard'}},
                          'verified_specs': {}}]}
    text = ProposalNarrative(headline='Предложение', opening='Предлагаем рассмотреть состав.',
                             solution='Маркер предназначен для досок.', closing='Подтвердите состав.')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response(review()))) as client:
        with pytest.raises(NarrativeReviewRejected):
            await review_proposal_narrative(json.dumps(data), text, GroqSettings(test_project_approved=True, api_key='offline'), client)


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['name', 'verified_specs'])
async def test_actual_selected_marker_properties_can_support_board_use(source: Literal['name', 'verified_specs']) -> None:
    product = {'name': 'Маркер для досок' if source == 'name' else 'Маркер черный',
               'verified_specs': {'marker_type': 'whiteboard'} if source == 'verified_specs' else {}}
    text = ProposalNarrative(headline='Предложение', opening='Предлагаем рассмотреть состав.',
                             solution='Маркер предназначен для досок.', closing='Подтвердите состав.')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response(review()))) as client:
        await review_proposal_narrative(json.dumps({'products': [product]}), text, GroqSettings(test_project_approved=True, api_key='offline'), client)


@pytest.mark.asyncio
@pytest.mark.parametrize(('claim', 'supported'), [
    ('Во втором варианте маркер предназначен для досок.', True),
    ('Вариант 2: маркер предназначен для досок.', True),
    ('Вариант 1: маркер предназначен для маркировки. Вариант 2: маркер предназначен для досок.', True),
    ('В первом варианте маркер предназначен для досок.', False),
    ('Вариант 2: маркер предназначен для досок. Вариант 1: маркер предназначен для досок.', False),
    ('Маркеры предназначены для досок.', False),
])
async def test_alternative_marker_evidence_stays_in_its_explicit_variant(claim: str, supported: bool) -> None:
    data = {'products': [{'product_id': 'variant:1:line:1', 'name': 'Маркер черный',
                          'verified_specs': {'marker_type': 'permanent'}}],
            'alternatives': [{'variant_number': 2, 'products': [
                {'product_id': 'variant:2:line:1', 'name': 'Маркер синий',
                 'verified_specs': {'marker_type': 'whiteboard'}},
            ]}]}
    text = ProposalNarrative(headline='Предложение', opening='Предлагаем рассмотреть составы.',
                             solution=claim, closing='Выберите вариант.')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response(review()))) as client:
        if supported:
            await review_proposal_narrative(json.dumps(data), text, GroqSettings(test_project_approved=True, api_key='offline'), client)
        else:
            with pytest.raises(NarrativeReviewRejected):
                await review_proposal_narrative(json.dumps(data), text, GroqSettings(test_project_approved=True, api_key='offline'), client)


@pytest.mark.asyncio
@pytest.mark.parametrize('primary_board', [False, True])
@pytest.mark.parametrize('claim', [
    'Вариант 2: маркер предназначен для досок. Маркеры обоих вариантов предназначены для досок.',
    'Вариант 2: маркер предназначен для досок, маркеры обоих вариантов предназначены для досок.',
    'Маркеры обоих вариантов предназначены для досок.',
])
async def test_explicit_variant_cannot_authorize_collective_claim_for_all_variants(claim: str, primary_board: bool) -> None:
    data = {'products': [{'name': 'Маркер', 'verified_specs': {'marker_type': 'whiteboard' if primary_board else 'permanent'}}],
            'alternatives': [{'variant_number': 2, 'products': [
                {'name': 'Маркер', 'verified_specs': {'marker_type': 'permanent' if primary_board else 'whiteboard'}},
            ]}]}
    text = ProposalNarrative(headline='Предложение', opening='Предлагаем рассмотреть составы.',
                             solution=claim, closing='Выберите вариант.')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response(review()))) as client:
        with pytest.raises(NarrativeReviewRejected):
            await review_proposal_narrative(json.dumps(data), text, GroqSettings(test_project_approved=True, api_key='offline'), client)


# Explicit offline accounting authority; production requires PostgreSQL.
pytestmark = pytest.mark.usefixtures("mock_llm_operation")
