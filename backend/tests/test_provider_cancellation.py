"""Cancellation must settle ledger threads before closing the operation."""
import asyncio
import threading
from typing import Any

import httpx
import pytest
from test_openrouter_client import Budget, infer, response

from kpdesk.db.provider_budget import ProviderAttempt, ProviderBudgetError
from kpdesk.llm.client import llm_operation


class BlockingBudget(Budget):
    def __init__(self, stage: str, *, reserve_denied: bool = False,
                 finish_failed: bool = False, close_failed: bool = False) -> None:
        super().__init__()
        self.stage = stage
        self.reserve_denied = reserve_denied
        self.finish_failed = finish_failed
        self.close_failed = close_failed
        self.entered = asyncio.Event()
        self.release = threading.Event()
        self.loop = asyncio.get_running_loop()
        self.events: list[str] = []

    def block(self, stage: str) -> None:
        self.events.append(stage + '_start')
        if stage == self.stage:
            self.loop.call_soon_threadsafe(self.entered.set)
            if not self.release.wait(5):
                raise AssertionError('Test controller did not release ledger thread')
        self.events.append(stage + '_end')

    def reserve(self, input_chars: int) -> ProviderAttempt:
        self.block('reserve')
        if self.reserve_denied:
            raise ProviderBudgetError('minute_quota')
        return super().reserve(input_chars)

    def finish(self, attempt: ProviderAttempt, **kwargs: Any) -> None:
        self.block('finish')
        if self.finish_failed:
            raise RuntimeError('synthetic-private-ledger-diagnostic')
        super().finish(attempt, **kwargs)

    def close(self, **kwargs: Any) -> None:
        self.block('close')
        if self.close_failed:
            raise RuntimeError('synthetic-private-close-diagnostic')
        super().close(**kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['reserve', 'finish', 'close'])
async def test_repeated_cancellation_settles_ledger_before_return(stage: str) -> None:
    budget = BlockingBudget(stage)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async def operation() -> None:
            async with llm_operation(budget):
                await infer(client)

        task = asyncio.create_task(operation())
        try:
            await asyncio.wait_for(budget.entered.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done(), 'Request returned with a ledger thread still running'
            if stage != 'close':
                assert 'close_start' not in budget.events, 'Operation closed before ledger settled'
        finally:
            budget.release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        assert budget.attempts == 1
        assert len(budget.finishes) == len(budget.closes) == 1
        assert budget.events.index('reserve_end') < budget.events.index('finish_start')
        assert budget.events.index('finish_end') < budget.events.index('close_start')
        if stage == 'reserve':
            assert not requests, 'Cancelled reservation dispatched a provider request'
            assert budget.finishes[0]['outcome'] == 'cancelled'
            assert budget.closes == [{'succeeded': False, 'ambiguous': True}]
        else:
            assert len(requests) == 1
            assert budget.finishes[0]['outcome'] == 'success'


@pytest.mark.asyncio
@pytest.mark.parametrize('reserve_denied,finish_failed', [(True, False), (False, True)])
async def test_cancelled_reserve_keeps_cancellation_when_ledger_fails(
    reserve_denied: bool, finish_failed: bool, caplog: pytest.LogCaptureFixture,
) -> None:
    budget = BlockingBudget('reserve', reserve_denied=reserve_denied, finish_failed=finish_failed)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async def operation() -> None:
            async with llm_operation(budget):
                await infer(client)

        task = asyncio.create_task(operation())
        try:
            await asyncio.wait_for(budget.entered.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            budget.release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
    assert not requests
    assert budget.attempts == (0 if reserve_denied else 1)
    assert len(budget.closes) == (0 if reserve_denied else 1)
    assert 'synthetic-private-ledger-diagnostic' not in caplog.text
    assert 'exception in shielded future' not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('finish_failed,close_failed', [(True, False), (False, True), (True, True)])
async def test_transport_cancellation_survives_ledger_cleanup_failure(
    finish_failed: bool, close_failed: bool, caplog: pytest.LogCaptureFixture,
) -> None:
    budget = BlockingBudget('none', finish_failed=finish_failed, close_failed=close_failed)

    def cancelled(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    async with httpx.AsyncClient(transport=httpx.MockTransport(cancelled)) as client:
        with pytest.raises(asyncio.CancelledError):
            async with llm_operation(budget):
                await infer(client)
    assert budget.events == ['reserve_start', 'reserve_end', 'finish_start', 'finish_end',
                             'close_start', 'close_end']
    if not close_failed:
        assert budget.closes == [{'succeeded': False, 'ambiguous': True}]
    assert 'synthetic-private' not in caplog.text
