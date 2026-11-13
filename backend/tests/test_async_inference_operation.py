"""Logical inference SQL must not block the API event loop."""
import asyncio
import inspect
import threading
from collections.abc import Callable
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from llm_test_support import MockBudget
from sqlalchemy import Engine

from kpdesk.api import universal_routes as routes
from kpdesk.llm.settings import LLMSettings


@pytest.mark.asyncio
@pytest.mark.parametrize('followup', [False, True])
async def test_logical_budget_runs_off_loop_and_closes_stages_in_order(
    monkeypatch: pytest.MonkeyPatch, followup: bool,
) -> None:
    loop_thread = threading.get_ident()
    events: list[tuple[str, int, Any]] = []
    reserved: list[UUID] = []

    def reserve(*args: object, **kwargs: Any) -> UUID:
        attempt = uuid4()
        reserved.append(attempt)
        events.append(('reserve', threading.get_ident(), kwargs['input_chars']))
        return attempt

    def finish(*args: object, **kwargs: Any) -> None:
        events.append(('finish', threading.get_ident(), (kwargs['request_id'], kwargs['succeeded'])))

    monkeypatch.setattr(routes, 'reserve_inference', reserve)
    monkeypatch.setattr(routes, 'finish_inference', finish)
    monkeypatch.setattr(routes, 'operation_budget', lambda *a, **k: MockBudget())

    async def run(before: Callable[..., object]) -> str:
        if followup:
            result = before('review', True)
            if inspect.isawaitable(result):
                await result
        return 'validated'

    result = await routes._provider_operation(cast(Engine, object()), uuid4(), 'cookie', 'csrf',
        str(uuid4()), LLMSettings(api_key='offline', test_project_approved=True), MockBudget,
        'synthetic', 'writer', run)
    assert result == 'validated'
    assert all(worker != loop_thread for _, worker, _ in events), 'Logical SQL blocked the API loop'
    assert [event[0] for event in events] == (['reserve', 'finish'] * (2 if followup else 1))
    assert [event[2] for event in events if event[0] == 'finish'] == [(item, True) for item in reserved]


@pytest.mark.asyncio
async def test_cancelled_late_logical_reservation_is_finished_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    reservation = uuid4()
    finishes: list[tuple[UUID, bool]] = []
    dispatched = []

    def reserve(*args: object, **kwargs: object) -> UUID:
        # Fail immediately rather than blocking pytest's loop in the old code.
        assert threading.get_ident() != loop_thread, 'SQL reservation ran on the API loop'
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(3)
        return reservation

    def finish(*args: object, **kwargs: Any) -> None:
        finishes.append((kwargs['request_id'], kwargs['succeeded']))

    async def run(before: Callable[..., object]) -> str:
        dispatched.append(True)
        return 'unexpected'

    loop_thread = threading.get_ident()
    monkeypatch.setattr(routes, 'reserve_inference', reserve)
    monkeypatch.setattr(routes, 'finish_inference', finish)
    monkeypatch.setattr(routes, 'operation_budget', lambda *a, **k: MockBudget())
    task = asyncio.create_task(routes._provider_operation(cast(Engine, object()), uuid4(), 'cookie',
        'csrf', str(uuid4()), LLMSettings(api_key='offline', test_project_approved=True), MockBudget,
        'synthetic', 'writer', run))
    watcher = asyncio.create_task(entered.wait())
    try:
        done, _ = await asyncio.wait([task, watcher], timeout=2,
                                     return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
        assert entered.is_set()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        if not task.done():
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
    assert not dispatched
    assert finishes == [(reservation, False)]
