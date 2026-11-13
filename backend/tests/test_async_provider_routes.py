"""Active tenant AI routes must keep blocking logical SQL off the event loop."""
import asyncio
import inspect
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest
from fastapi import Response
from llm_test_support import MockBudget
from sqlalchemy import Engine
from test_narrative_review import TEXT

from kpdesk.api import tenant_document_routes as documents
from kpdesk.api import tenant_extraction_routes as extraction
from kpdesk.db.llm_cache import CacheClaim
from kpdesk.extraction.schema import ExtractedRequest
from kpdesk.llm.settings import LLMSettings


@pytest.mark.asyncio
@pytest.mark.parametrize('route', ['document', 'extraction'])
async def test_tenant_sql_stages_are_not_on_event_loop(monkeypatch: pytest.MonkeyPatch, route: str) -> None:
    loop_thread = threading.get_ident()
    threads: list[tuple[str, int]] = []
    module = documents if route == 'document' else extraction

    def reserve(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        threads.append(('reserve', threading.get_ident()))
        return uuid4()

    def finish(*args: object, **kwargs: object) -> None:
        threads.append(('finish', threading.get_ident()))

    monkeypatch.setattr(module, 'reserve_inference', reserve)
    monkeypatch.setattr(module, 'finish_inference', finish)
    monkeypatch.setattr(module, 'operation_budget', lambda *a, **k: MockBudget())
    engine = MagicMock(spec=Engine)
    settings = LLMSettings(api_key='offline', test_project_approved=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as client:
        if route == 'document':
            quote = SimpleNamespace(quote_id=uuid4(), revision=1, snapshot_sha256='a' * 64)
            store = MagicMock()
            store.get.return_value = quote
            monkeypatch.setattr(documents, '_store', lambda *a, **k: store)
            monkeypatch.setattr(documents, 'proposal_facts', lambda *a, **k: '{}')

            async def narrative(*args: object, **kwargs: Any):  # type: ignore[no-untyped-def]
                callback = kwargs['before_followup']('review', True)
                if inspect.isawaitable(callback):
                    await callback
                return TEXT

            monkeypatch.setattr(documents, 'generate_proposal_narrative', narrative)
            await documents.preview_presentation(uuid4(), quote.quote_id,
                documents.PresentationBody(issuer_name='Synthetic', manager_brief='Synthetic',
                    expected_revision=1, source_snapshot_sha256='a' * 64, consent_to_external_processing=True),
                Response(), engine, settings, client, MockBudget, str(uuid4()), 's' * 43, 'csrf')
        else:
            monkeypatch.setattr(extraction, 'cache_secret', lambda: None)

            async def extract(*args: object, **kwargs: object) -> ExtractedRequest:
                return ExtractedRequest(lines=[])

            def seal(*args: object, **kwargs: object) -> None:
                threads.append(('seal', threading.get_ident()))

            monkeypatch.setattr(extraction, 'extract_request', extract)
            monkeypatch.setattr(extraction, 'seal_inference_preview', seal)
            await extraction.extract_tenant_request(uuid4(), extraction.TenantExtractText(text='synthetic',
                consent_to_external_processing=True), Response(), engine, settings, client,
                MockBudget, str(uuid4()), 's' * 43, 'csrf')
    assert threads
    assert all(worker != loop_thread for _, worker in threads), 'Tenant logical SQL blocked API loop'


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['reserve', 'claim', 'finish', 'seal', 'publish'])
async def test_extraction_cancellation_settles_and_releases_owned_pending_cache(
    monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    entered = asyncio.Event()
    release = threading.Event()
    events: list[str] = []
    attempt = uuid4()

    def step(name: str) -> None:
        assert threading.get_ident() != loop_thread
        events.append(name)
        if name == stage:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(3)

    def reserve(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        step('reserve')
        return attempt

    def finish(*args: object, **kwargs: object) -> None:
        step('finish')

    def claim(*args: object, **kwargs: object) -> CacheClaim:
        step('claim')
        return CacheClaim('miss', None)

    def seal(*args: object, **kwargs: object) -> None:
        step('seal')

    def publish(*args: object, **kwargs: object) -> bool:
        step('publish')
        return True

    def cleanup(*args: object, **kwargs: object) -> None:
        assert kwargs['attempt_id'] == attempt
        step('release')

    async def extract(*args: object, **kwargs: object) -> ExtractedRequest:
        events.append('extract')
        return ExtractedRequest(lines=[])

    monkeypatch.setattr(extraction, 'reserve_inference', reserve)
    monkeypatch.setattr(extraction, 'finish_inference', finish)
    monkeypatch.setattr(extraction, 'cache_secret', lambda: b'x' * 32)
    monkeypatch.setattr(extraction, 'claim_cached_extraction', claim)
    monkeypatch.setattr(extraction, 'seal_inference_preview', seal)
    monkeypatch.setattr(extraction, 'publish_cached_extraction', publish)
    monkeypatch.setattr(extraction, 'release_cached_extraction', cleanup)
    monkeypatch.setattr(extraction, 'extract_request', extract)
    monkeypatch.setattr(extraction, 'operation_budget', lambda *a, **k: MockBudget())
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as client:
        task = asyncio.create_task(extraction.extract_tenant_request(uuid4(),
            extraction.TenantExtractText(text='synthetic', consent_to_external_processing=True),
            Response(), MagicMock(spec=Engine), LLMSettings(api_key='offline', test_project_approved=True),
            client, MockBudget, str(uuid4()), 's' * 43, 'csrf'))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done(), 'Cancellation abandoned owned SQL worker'
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert events.count('finish') == 1
    assert events.count('release') == (0 if stage == 'reserve' else 1)
    if stage in {'reserve', 'claim'}:
        assert 'extract' not in events
