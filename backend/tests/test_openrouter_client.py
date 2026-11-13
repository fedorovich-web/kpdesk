"""Offline wire, privacy, accounting and bounded-replay contract."""
import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, NoReturn
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from kpdesk.db.provider_budget import ProviderAttempt, ProviderBudgetError
from kpdesk.llm.client import LLMError, llm_operation, structured_inference
from kpdesk.llm.settings import MODEL, LLMSettings

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}},
          "required": ["ok"], "additionalProperties": False}
SETTINGS = LLMSettings(api_key="synthetic-not-a-key", test_project_approved=True)


@dataclass
class Budget:
    attempts: int = 0
    finishes: list[dict[str, Any]] = field(default_factory=list)
    closes: list[dict[str, Any]] = field(default_factory=list)
    denial: str | None = None

    def reserve(self, input_chars: int) -> ProviderAttempt:
        if self.denial:
            raise ProviderBudgetError(self.denial, 60)
        self.attempts += 1
        return ProviderAttempt(uuid4(), self.attempts)

    def finish(self, attempt: ProviderAttempt, **kwargs: Any) -> None:
        self.finishes.append(kwargs)

    def close(self, **kwargs: Any) -> None:
        self.closes.append(kwargs)


def response(content: str = '{"ok":true}', *, model: str = MODEL,
             cost: int | str = 0, reason: str = "tool_calls") -> dict[str, Any]:
    return {"model": model, "choices": [{"finish_reason": reason, "message": {
        "tool_calls": [{"type": "function", "function": {
            "name": "test_result", "arguments": content,
        }}]}}], "usage": {"cost": cost, "prompt_tokens": 10,
                          "completion_tokens": 5, "total_tokens": 15}}


async def infer(client: httpx.AsyncClient, settings: LLMSettings = SETTINGS) -> Any:
    return await structured_inference("synthetic", settings, client,
        system_prompt="Test", schema_name="test_result", schema=SCHEMA, parse=json.loads)


@pytest.mark.asyncio
async def test_fixed_free_wire_and_multiple_operation_stages() -> None:
    requests = []
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=response())
    budget = Budget()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client, llm_operation(budget):
        assert await infer(client) == {"ok": True}
        assert await infer(client) == {"ok": True}
    assert budget.attempts == 2
    assert budget.closes == [{"succeeded": True, "ambiguous": False}]
    body = json.loads(requests[0].content)
    assert str(requests[0].url) == "https://openrouter.ai/api/v1/chat/completions"
    assert body["model"] == MODEL
    assert body["provider"] == {"only": ["nvidia"], "allow_fallbacks": False, "max_price": {
        "prompt": 0, "completion": 0, "request": 0}}
    assert "response_format" not in body and "models" not in body
    assert body["tool_choice"]["function"]["name"] == "test_result"
    assert all(x["outcome"] == "success" and x["total_tokens"] == 15 for x in budget.finishes)


@pytest.mark.asyncio
async def test_explicit_real_catalog_configuration_uses_same_budgeted_nvidia_route(tmp_path: Any) -> None:
    from typing import cast

    from sqlalchemy import Engine

    from kpdesk.api.provider_scope import operation_budget

    env = tmp_path / 'provider.env'
    env.write_text('OPENROUTER_API_KEY=offline-test\nOPENROUTER_DATA_MODE=real_catalogs\n',
                   encoding='utf-8')
    settings = LLMSettings.from_local_env(env)
    assert not settings.test_project_approved
    budget = Budget()
    scoped = operation_budget(
        lambda *args, **kwargs: budget, cast(Engine, object()), organization_id=uuid4(),
        cookie='session', csrf='csrf', key=str(uuid4()), purpose='extraction',
        payload='Линейка металлическая 50 см', settings=settings,
    )
    requests = []
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client, llm_operation(scoped):
        assert await infer(client, settings) == {'ok': True}
    assert budget.attempts == 1 and budget.finishes[0]['outcome'] == 'success'
    assert requests[0]['model'] == MODEL
    assert requests[0]['provider']['only'] == ['nvidia']
    assert requests[0]['provider']['allow_fallbacks'] is False
    assert all(value == 0 for value in requests[0]['provider']['max_price'].values())


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code,attempts", [
    (400, "provider_error", 1), (401, "provider_auth_failed", 1),
    (403, "provider_auth_failed", 1), (404, "provider_model_unavailable", 1),
    (429, "rate_limited", 2), (500, "provider_error", 2), (503, "provider_error", 2),
])
async def test_http_failures_are_bounded(status: int, code: str, attempts: int,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_wait(_: float) -> None:
        pass
    monkeypatch.setattr("kpdesk.llm.client.asyncio.sleep", no_wait)
    budget = Budget()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(status, headers={"Retry-After": "0"},
                                 json={"error": {"message": "DO NOT EXPOSE"}}),
    )) as client:
        with pytest.raises(LLMError, match=code):
            async with llm_operation(budget):
                await infer(client)
    assert budget.attempts == attempts
    assert len(budget.finishes) == attempts


@pytest.mark.asyncio
async def test_large_retry_after_returns_without_replay() -> None:
    budget = Budget()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(429, headers={"Retry-After": "600"}),
    )) as client:
        with pytest.raises(LLMError) as caught:
            async with llm_operation(budget):
                await infer(client)
    assert caught.value.retry_after_seconds == 600 and budget.attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ['{}', '{"ok":"true"}', '{"ok":true,"ok":false}', '', '```json\n{}\n```'])
async def test_one_format_repair_then_valid(invalid: str) -> None:
    budget = Budget()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=response(invalid if budget.attempts == 1 else '{"ok":true}')),
    )) as client, llm_operation(budget):
        assert await infer(client) == {"ok": True}
    assert [x["outcome"] for x in budget.finishes] == ["invalid_output", "success"]


@pytest.mark.asyncio
async def test_persistent_invalid_stops_after_two() -> None:
    budget = Budget()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=response('{}')),
    )) as client:
        with pytest.raises(LLMError, match="invalid_output"):
            async with llm_operation(budget):
                await infer(client)
    assert budget.attempts == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("exception", [httpx.ReadTimeout, httpx.ConnectError])
async def test_uncertain_transport_is_never_replayed(exception: type[httpx.TransportError]) -> None:
    def handler(request: httpx.Request) -> NoReturn:
        raise exception("sensitive", request=request)
    budget = Budget()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(LLMError):
            async with llm_operation(budget):
                await infer(client)
    assert budget.attempts == 1 and budget.finishes[0]["outcome"] == "ambiguous"
    assert budget.closes[0]["ambiguous"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("body,code", [
    (response(model="openai/paid"), "provider_identity_mismatch"),
    (response(cost="0.001"), "provider_cost_violation"),
    (response(cost="NaN"), "provider_cost_violation"),
])
async def test_identity_and_cost_rejected_without_repair(body: dict[str, Any], code: str) -> None:
    budget = Budget()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=body),
    )) as client:
        with pytest.raises(LLMError, match=code):
            async with llm_operation(budget):
                await infer(client)
    assert budget.attempts == 1


@pytest.mark.asyncio
async def test_missing_budget_and_privacy_approval_fail_before_network() -> None:
    def forbidden(_: httpx.Request) -> NoReturn:
        pytest.fail("Network must not run")
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        with pytest.raises(LLMError, match="provider_budget_required"):
            await infer(client)
        with pytest.raises(LLMError, match="synthetic_project_required"):
            async with llm_operation(Budget()):
                await infer(client, LLMSettings(api_key="test"))


@pytest.mark.asyncio
async def test_quota_denial_has_no_dispatch() -> None:
    budget = Budget(denial="day_quota")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("No dispatch"))) as client:
        with pytest.raises(LLMError, match="inference_quota_exceeded"):
            async with llm_operation(budget):
                await infer(client)
    assert not budget.finishes and not budget.closes


@pytest.mark.parametrize("model", ["openrouter/free", "openrouter/auto", "nvidia/nemotron-3-ultra-550b-a55b", "openai/gpt-oss-120b"])
def test_paid_or_router_settings_forbidden(model: str) -> None:
    with pytest.raises(ValidationError):
        LLMSettings.model_validate({"api_key": "test", "model": model})


@pytest.mark.asyncio
async def test_quote_operation_cap_counts_all_stages() -> None:
    budget = Budget()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=response()),
    )) as client:
        with pytest.raises(LLMError, match="inference_quota_exceeded"):
            async with llm_operation(budget):
                for _ in range(7):
                    await infer(client)
    assert budget.attempts == 6 and len(budget.finishes) == 6


@pytest.mark.asyncio
async def test_cancellation_records_attempt_and_poisons_operation() -> None:
    def cancelled(_: httpx.Request) -> NoReturn:
        raise asyncio.CancelledError
    budget = Budget()
    async with httpx.AsyncClient(transport=httpx.MockTransport(cancelled)) as client:
        with pytest.raises(asyncio.CancelledError):
            async with llm_operation(budget):
                await infer(client)
    assert budget.attempts == 1 and budget.finishes[0]["outcome"] == "cancelled"
    assert budget.closes == [{"succeeded": False, "ambiguous": True}]
