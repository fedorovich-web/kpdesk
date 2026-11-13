"""A draft is returned only after every section passes a separate facts review."""
import json

import httpx
import pytest

from kpdesk.extraction.groq import GroqExtractionError, GroqSettings
from kpdesk.quotes.document import ProposalNarrative
from kpdesk.quotes.narrative import generate_proposal_narrative

TEXT = ProposalNarrative(headline='Предложение для офиса', opening='Предлагаем рассмотреть состав.',
                         solution='Маркеры предназначены для маркировки.',
                         closing='Подтвердите состав и согласуем условия поставки.')
FACTS = json.dumps({'manager_brief': 'Офис на 12 рабочих мест.',
                    'products': [{'name': 'Перманентный черный маркер', 'quantity': 3, 'unit': 'штука'}]})


def review() -> dict[str, object]:
    return {name: {'status': 'supported', 'issues': []} for name in ProposalNarrative.model_fields}


def response(payload: object) -> httpx.Response:
    return httpx.Response(200, json={"model": "nvidia/nemotron-3-ultra-550b-a55b:free", 'choices': [{'finish_reason': 'stop',
                         'message': {'content': json.dumps(payload, ensure_ascii=False)}}]})


def draft_text(content: str) -> dict[str, str]:
    payload = json.loads(content)['draft']
    return {name: ''.join(part['text'] for part in parts) for name, parts in payload.items()}


@pytest.mark.asyncio
async def test_negative_semantic_review_still_blocks_after_one_bounded_revision() -> None:
    calls = []
    finding = review()
    finding['solution'] = {'status': 'unsupported', 'issues': [
        {'passage_id': 'solution:0', 'reason': 'unsupported_property'},
    ]}

    def provider(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) % 2:
            return response({'solution': TEXT.solution} if len(calls) == 3 else TEXT.model_dump())
        assert json.loads(body['messages'][-1]['content'])['facts'] == json.loads(FACTS)
        assert draft_text(body['messages'][-1]['content']) == TEXT.model_dump()
        return response(finding)

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        with pytest.raises(GroqExtractionError, match='unverified_narrative'):
            await generate_proposal_narrative(FACTS, GroqSettings(test_project_approved=True, api_key='offline'), client)
    assert len(calls) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize('bad_review', ['missing_section', 'uncertain', 'foreign_passage', 'false_success'])
async def test_missing_uncertain_or_inconsistent_review_never_succeeds(bad_review: str) -> None:
    verdict = review()
    if bad_review == 'missing_section':
        del verdict['closing']
    elif bad_review == 'uncertain':
        verdict['solution'] = {'status': 'uncertain', 'issues': []}
    else:
        verdict['solution'] = {'status': 'supported' if bad_review == 'false_success' else 'unsupported',
                               'issues': [{'passage_id': 'opening:0' if bad_review == 'foreign_passage' else 'solution:0',
                                           'reason': 'unsupported_property'}]}
    attempts = []
    def provider(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return response(TEXT.model_dump() if len(attempts) == 1 else verdict)
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        with pytest.raises(GroqExtractionError):
            await generate_proposal_narrative(FACTS, GroqSettings(test_project_approved=True, api_key='offline'), client)
    assert len(attempts) == (2 if bad_review in {'uncertain', 'false_success'} else 3)


@pytest.mark.asyncio
async def test_quota_gate_runs_before_review_http_and_can_stop_it() -> None:
    calls = []
    def provider(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(TEXT.model_dump())
    def deny_review(text: str, previous_succeeded: bool) -> None:
        assert previous_succeeded
        assert draft_text(text) == TEXT.model_dump()
        raise RuntimeError('quota denied')
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        with pytest.raises(RuntimeError, match='quota denied'):
            await generate_proposal_narrative(FACTS, GroqSettings(test_project_approved=True, api_key='offline'), client,
                                              before_followup=deny_review)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_complete_supported_review_returns_original_draft() -> None:
    calls = []
    safe = TEXT.model_copy(update={'solution': 'В состав включены черные маркеры для маркировки.'})
    def provider(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(safe.model_dump() if len(calls) == 1 else review())
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        result = await generate_proposal_narrative(FACTS, GroqSettings(test_project_approved=True, api_key='offline'), client)
    assert result == safe
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_review_failure_cannot_return_unchecked_writer_result() -> None:
    calls = []
    def provider(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(TEXT.model_dump()) if len(calls) == 1 else httpx.Response(503)
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        with pytest.raises(GroqExtractionError, match='provider_error'):
            await generate_proposal_narrative(FACTS, GroqSettings(test_project_approved=True, api_key='offline'), client)
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_oversized_review_context_stops_before_second_reservation_or_http() -> None:
    calls = []
    gates: list[str] = []
    def provider(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(TEXT.model_dump())
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        with pytest.raises(GroqExtractionError, match='review_context_too_large'):
            await generate_proposal_narrative(json.dumps({'brief': 'x' * 11_900}),
                                              GroqSettings(test_project_approved=True, api_key='offline'), client,
                                              before_followup=lambda text, succeeded: gates.append(text))
    assert len(calls) == 1 and not gates


@pytest.mark.asyncio
async def test_duplicate_review_fields_cannot_overwrite_a_negative_status() -> None:
    calls = []
    verdict = json.dumps(review()).replace('"status": "supported"',
                                            '"status": "unsupported", "status": "supported"', 1)
    def provider(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return response(TEXT.model_dump())
        return httpx.Response(200, json={"model": "nvidia/nemotron-3-ultra-550b-a55b:free", 'choices': [{'finish_reason': 'stop',
                             'message': {'content': verdict}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        with pytest.raises(GroqExtractionError, match='invalid_output'):
            await generate_proposal_narrative(FACTS, GroqSettings(test_project_approved=True, api_key='offline'), client)


# Explicit offline accounting authority; production requires PostgreSQL.
pytestmark = pytest.mark.usefixtures("mock_llm_operation")
