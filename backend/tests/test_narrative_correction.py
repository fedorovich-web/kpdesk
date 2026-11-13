"""One evidence-guided revision, four calls maximum, no network retries."""
import asyncio
import json

import httpx
import pytest
from test_narrative_review import FACTS, TEXT, draft_text, response, review

from kpdesk.extraction.groq import GroqExtractionError, GroqSettings
from kpdesk.quotes.narrative import generate_proposal_narrative

FIXED = TEXT.model_copy(update={'solution': 'Включены три черных перманентных маркера для маркировки.'})


def negative() -> dict[str, object]:
    result = review()
    result['solution'] = {'status': 'unsupported', 'issues': [
        {'passage_id': 'solution:0', 'reason': 'unsupported_property'},
    ]}
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize('async_gate', [False, True])
async def test_one_revision_uses_original_facts_and_checked_feedback_then_passes_review(async_gate: bool) -> None:
    bodies = []
    stages = []
    def gate(text: str, previous_succeeded: bool) -> None:
        stages.append(previous_succeeded)
        assert json.loads(text)['facts'] == json.loads(FACTS)
    async def awaited_gate(text: str, previous_succeeded: bool) -> None:
        await asyncio.sleep(0)
        gate(text, previous_succeeded)
    def provider(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        assert len(stages) == len(bodies) - 1, 'Provider dispatched before logical quota gate completed'
        if len(bodies) == 3:
            revision = json.loads(body['messages'][-1]['content'])
            assert revision['draft'] == TEXT.model_dump()
            assert revision['review']['solution']['issues'] == [
                {'quote': TEXT.solution, 'reason': 'unsupported_property'},
            ]
        if len(bodies) == 4:
            assert draft_text(body['messages'][-1]['content']) == FIXED.model_dump()
        return response([TEXT.model_dump(), negative(), {'solution': FIXED.solution}, review()][len(bodies)-1])
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        result = await generate_proposal_narrative(FACTS, GroqSettings(test_project_approved=True, api_key='offline'), client,
                                                  before_followup=awaited_gate if async_gate else gate)
    assert result == FIXED
    assert stages == [True, False, True]
    assert len(bodies) == 4


@pytest.mark.asyncio
async def test_second_negative_review_ends_after_four_calls_without_returning_any_draft() -> None:
    attempts = []
    def provider(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return response({'solution': TEXT.solution} if len(attempts) == 3 else TEXT.model_dump() if len(attempts) == 1 else negative())
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        with pytest.raises(GroqExtractionError, match='unverified_narrative'):
            await generate_proposal_narrative(FACTS, GroqSettings(test_project_approved=True, api_key='offline'), client)
    assert len(attempts) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize('stop_at', [3, 4])
async def test_each_followup_requires_quota_before_http(stop_at: int) -> None:
    attempts: list[httpx.Request] = []
    def gate(text: str, previous_succeeded: bool) -> None:
        if len(attempts) + 1 == stop_at:
            raise RuntimeError('quota denied')
    def provider(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return response([TEXT.model_dump(), negative(), {'solution': FIXED.solution}][len(attempts)-1])
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        with pytest.raises(RuntimeError, match='quota denied'):
            await generate_proposal_narrative(FACTS, GroqSettings(test_project_approved=True, api_key='offline'), client,
                                              before_followup=gate)
    assert len(attempts) == stop_at - 1


@pytest.mark.asyncio
@pytest.mark.parametrize('fail_at', [3, 4])
async def test_network_failure_during_revision_or_final_review_has_no_retry(fail_at: int) -> None:
    attempts = []
    def provider(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) == fail_at:
            return httpx.Response(429, headers={'Retry-After': '12'})
        return response([TEXT.model_dump(), negative(), {'solution': FIXED.solution}][len(attempts)-1])
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        with pytest.raises(GroqExtractionError, match='rate_limited'):
            await generate_proposal_narrative(FACTS, GroqSettings(test_project_approved=True, api_key='offline'), client)
    assert len(attempts) == fail_at


@pytest.mark.asyncio
async def test_oversized_correction_does_not_reserve_or_send_a_third_request() -> None:
    attempts = []
    stages = []
    findings = review()
    findings['solution'] = {'status': 'unsupported', 'issues': [
        {'passage_id': 'solution:0', 'reason': 'unsupported_property'} for _ in range(8)
    ]}
    def provider(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return response(TEXT.model_dump() if len(attempts) == 1 else findings)
    def gate(text: str, succeeded: bool) -> None:
        stages.append(succeeded)
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        with pytest.raises(GroqExtractionError, match='correction_context_too_large'):
            await generate_proposal_narrative(json.dumps({'brief': 'x' * 11300}),
                                              GroqSettings(test_project_approved=True, api_key='offline'), client,
                                              before_followup=gate)
    assert len(attempts) == 2 and stages == [True]


# Explicit offline accounting authority; production requires PostgreSQL.
pytestmark = pytest.mark.usefixtures("mock_llm_operation")
