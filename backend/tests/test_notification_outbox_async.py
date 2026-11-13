"""Offline dispatch contracts with synthetic envelopes and an in-memory SQL boundary."""
import asyncio
import threading
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import ModuleType, SimpleNamespace
from typing import cast
from uuid import UUID

import httpx
import pytest
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Engine

from kpdesk.notifications import account_email_outbox, resend_mail, team_invite_outbox
from kpdesk.notifications.resend_mail import EmailSettings

IDENTIFIER = UUID("12345678-1234-1234-1234-123456789012")
TOKEN = "a" * 43
type Dispatch = Callable[..., Coroutine[None, None, str]]


@dataclass
class SqlBoundary:
    ciphertext: str
    rows: bool = True
    acknowledged: bool = True
    claim_hook: Callable[[], None] | None = None
    complete_hook: Callable[[], None] | None = None
    transactions: list[list[str]] = field(default_factory=list)
    thread_ids: list[int] = field(default_factory=list)
    completion: list[dict[str, object]] = field(default_factory=list)
    closed: int = 0

    @contextmanager
    def begin(self) -> Iterator[SimpleNamespace]:
        owner = threading.get_ident()
        self.thread_ids.append(owner)
        statements: list[str] = []
        self.transactions.append(statements)

        def local(statement: str) -> None:
            assert threading.get_ident() == owner
            statements.append(statement)

        def claim(statement: object) -> SimpleNamespace:
            assert threading.get_ident() == owner
            statements.append(str(statement))
            if self.claim_hook:
                self.claim_hook()
            row = SimpleNamespace(
                challenge_id=IDENTIFIER, recipient_email="tester@example.org", purpose="signup",
                token_ciphertext=self.ciphertext, invite_id=IDENTIFIER,
                recipient="tester@example.org", ciphertext=self.ciphertext,
            )
            return SimpleNamespace(one_or_none=lambda: row if self.rows else None)

        def complete(statement: object, params: dict[str, object]) -> bool:
            assert threading.get_ident() == owner
            statements.append(str(statement))
            self.completion.append(params)
            if self.complete_hook:
                self.complete_hook()
            return self.acknowledged

        try:
            yield SimpleNamespace(exec_driver_sql=local, execute=claim, scalar=complete)
        finally:
            assert threading.get_ident() == owner
            self.closed += 1


@pytest.fixture(params=["account", "team"])
def dispatcher(request: pytest.FixtureRequest) -> tuple[ModuleType, Dispatch]:
    if request.param == "account":
        return account_email_outbox, account_email_outbox.dispatch_one
    return team_invite_outbox, team_invite_outbox.dispatch_team_one


def setup_boundary(monkeypatch: pytest.MonkeyPatch, module: ModuleType) -> SqlBoundary:
    cipher = Fernet(Fernet.generate_key())
    monkeypatch.setattr(module, "_cipher", lambda: cipher)
    return SqlBoundary(cipher.encrypt(TOKEN.encode("ascii")).decode("ascii"))


def test_dispatch_sql_transactions_leave_event_loop_and_keep_connections_owned(
    monkeypatch: pytest.MonkeyPatch, dispatcher: tuple[ModuleType, Dispatch],
) -> None:
    module, dispatch = dispatcher
    sql = setup_boundary(monkeypatch, module)
    loop_thread = threading.get_ident()
    sent: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        assert sql.closed == 1
        return httpx.Response(201, json={"id": "offline-provider-id"})

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            result = await dispatch(
                cast(Engine, sql), client=client, settings=EmailSettings("re_synthetic"),
            )
            assert result == "sent"
            assert not client.is_closed

    asyncio.run(scenario())
    assert len(sent) == 1
    assert sent[0].headers["Idempotency-Key"] in {
        "account-12345678-1234-1234-1234-123456789012",
        "invite-12345678-1234-1234-1234-123456789012",
    }
    assert sql.closed == 2
    assert len(sql.thread_ids) == 2
    assert all(identifier != loop_thread for identifier in sql.thread_ids)
    assert sql.completion == [{"id": IDENTIFIER, "provider": "offline-provider-id", "success": True}]


def test_dispatch_bounds_sql_waits_before_claim_and_completion(
    monkeypatch: pytest.MonkeyPatch, dispatcher: tuple[ModuleType, Dispatch],
) -> None:
    module, dispatch = dispatcher
    sql = setup_boundary(monkeypatch, module)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"id": "offline-id"}),
        )) as client:
            await dispatch(cast(Engine, sql), client=client, settings=EmailSettings("re_synthetic"))

    asyncio.run(scenario())
    assert len(sql.transactions) == 2
    for statements in sql.transactions:
        assert statements[:2] == ["SET LOCAL statement_timeout='10s'", "SET LOCAL lock_timeout='5s'"]


def test_cancellation_during_claim_settles_transaction_without_provider_send(
    monkeypatch: pytest.MonkeyPatch, dispatcher: tuple[ModuleType, Dispatch],
) -> None:
    module, dispatch = dispatcher
    sql = setup_boundary(monkeypatch, module)
    sent: list[httpx.Request] = []
    release = threading.Event()

    async def scenario() -> None:
        loop = asyncio.get_running_loop()

        def during_claim() -> None:
            loop.call_soon_threadsafe(task.cancel)
            loop.call_soon_threadsafe(release.set)
            assert release.wait(2), "SQL claim blocked the event loop"

        sql.claim_hook = during_claim

        def provider(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return httpx.Response(200, json={"id": "must-not-send"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            task = asyncio.create_task(dispatch(
                cast(Engine, sql), client=client, settings=EmailSettings("re_synthetic"),
            ))
            with pytest.raises(asyncio.CancelledError):
                await task
            assert sql.closed == 1
            assert not client.is_closed

    asyncio.run(scenario())
    assert sent == []
    assert sql.completion == []


@pytest.mark.parametrize("provider_status", [200, 429])
def test_provider_result_is_acknowledged_truthfully(
    monkeypatch: pytest.MonkeyPatch, dispatcher: tuple[ModuleType, Dispatch], provider_status: int,
) -> None:
    module, dispatch = dispatcher
    sql = setup_boundary(monkeypatch, module)

    async def scenario() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(provider_status, json={"id": "offline-id"}),
        )) as client:
            return await dispatch(cast(Engine, sql), client=client, settings=EmailSettings("re_synthetic"))

    if provider_status == 200:
        assert asyncio.run(scenario()) == "sent"
        assert sql.completion == [{"id": IDENTIFIER, "provider": "offline-id", "success": True}]
    else:
        assert asyncio.run(scenario()) == "failed"
        assert sql.completion == [{"id": IDENTIFIER, "provider": None, "success": False}]
    assert sql.closed == 2


def test_lost_sql_lease_never_returns_sent(
    monkeypatch: pytest.MonkeyPatch, dispatcher: tuple[ModuleType, Dispatch],
) -> None:
    module, dispatch = dispatcher
    sql = setup_boundary(monkeypatch, module)
    sql.acknowledged = False

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"id": "offline-id"}),
        )) as client:
            with pytest.raises(RuntimeError, match="lease expired"):
                await dispatch(cast(Engine, sql), client=client, settings=EmailSettings("re_synthetic"))
            assert not client.is_closed

    asyncio.run(scenario())
    assert sql.closed == 2


@pytest.mark.parametrize("completion_fails", [False, True])
def test_cancelled_completion_is_owned_until_closed_and_owned_client_is_closed(
    monkeypatch: pytest.MonkeyPatch, dispatcher: tuple[ModuleType, Dispatch], completion_fails: bool,
) -> None:
    module, dispatch = dispatcher
    sql = setup_boundary(monkeypatch, module)
    transport = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"id": "offline-id"}),
    ))
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda: transport)
    release = threading.Event()

    async def scenario() -> None:
        loop = asyncio.get_running_loop()

        def during_complete() -> None:
            loop.call_soon_threadsafe(task.cancel)
            loop.call_soon_threadsafe(task.cancel)
            loop.call_soon_threadsafe(release.set)
            assert release.wait(2), "SQL completion blocked the event loop"
            if completion_fails:
                raise RuntimeError("synthetic SQL failure")

        sql.complete_hook = during_complete
        task = asyncio.create_task(dispatch(cast(Engine, sql), settings=EmailSettings("re_synthetic")))
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sql.closed == 2
        assert transport.is_closed

    asyncio.run(scenario())
    assert sql.completion == [{"id": IDENTIFIER, "provider": "offline-id", "success": True}]


@pytest.mark.parametrize("no_rows", [False, True])
def test_decryption_failure_or_empty_queue_never_sends_or_acknowledges(
    monkeypatch: pytest.MonkeyPatch, dispatcher: tuple[ModuleType, Dispatch], no_rows: bool,
) -> None:
    module, dispatch = dispatcher
    sql = setup_boundary(monkeypatch, module)
    sql.rows = not no_rows
    sql.ciphertext = "invalid-envelope"
    sent: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"id": "must-not-send"})

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            if no_rows:
                assert await dispatch(cast(Engine, sql), client=client, settings=EmailSettings("re_synthetic")) == "empty"
            elif module is account_email_outbox:
                with pytest.raises(RuntimeError, match="Unable to decrypt account email envelope"):
                    await dispatch(cast(Engine, sql), client=client, settings=EmailSettings("re_synthetic"))
            else:
                with pytest.raises(InvalidToken):
                    await dispatch(cast(Engine, sql), client=client, settings=EmailSettings("re_synthetic"))

    asyncio.run(scenario())
    assert sql.closed == 1
    assert sent == []
    assert sql.completion == []


@pytest.mark.parametrize("cancel_during_completion", [False, True])
@pytest.mark.parametrize("close_fails", [False, True])
def test_cancellation_during_owned_client_cleanup_does_not_abandon_close(
    monkeypatch: pytest.MonkeyPatch, dispatcher: tuple[ModuleType, Dispatch],
    cancel_during_completion: bool, close_fails: bool,
) -> None:
    module, dispatch = dispatcher
    sql = setup_boundary(monkeypatch, module)

    async def scenario() -> None:
        close_started = asyncio.Event()
        release_close = asyncio.Event()
        loop = asyncio.get_running_loop()
        task: asyncio.Task[str]

        if cancel_during_completion:
            def cancel_completion() -> None:
                loop.call_soon_threadsafe(task.cancel, "original")

            sql.complete_hook = cancel_completion

        class Transport(httpx.AsyncBaseTransport):
            closed = False

            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json={"id": "offline-id"})

            async def aclose(self) -> None:
                close_started.set()
                await release_close.wait()
                self.closed = True
                if close_fails:
                    raise RuntimeError("synthetic close failure")

        boundary = Transport()
        client = httpx.AsyncClient(transport=boundary)
        monkeypatch.setattr(module.httpx, "AsyncClient", lambda: client)
        task = asyncio.create_task(dispatch(cast(Engine, sql), settings=EmailSettings("re_synthetic")))
        await asyncio.wait_for(close_started.wait(), timeout=2)
        task.cancel("later")
        task.cancel("latest")
        loop.call_soon(release_close.set)
        with pytest.raises(asyncio.CancelledError) as error:
            await task
        assert boundary.closed
        assert error.value.args == ("original" if cancel_during_completion else "later",)

    asyncio.run(scenario())
    assert sql.closed == 2
    assert sql.completion == [{"id": IDENTIFIER, "provider": "offline-id", "success": True}]


def test_owned_client_close_timeout_is_bounded_and_never_returns_sent(
    monkeypatch: pytest.MonkeyPatch, dispatcher: tuple[ModuleType, Dispatch],
) -> None:
    module, dispatch = dispatcher
    sql = setup_boundary(monkeypatch, module)
    monkeypatch.setattr(resend_mail, "_CLIENT_CLOSE_TIMEOUT_SECONDS", 0.01)

    async def scenario() -> None:
        close_cancelled = asyncio.Event()

        class Transport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json={"id": "offline-id"})

            async def aclose(self) -> None:
                try:
                    await asyncio.Event().wait()
                finally:
                    close_cancelled.set()

        client = httpx.AsyncClient(transport=Transport())
        monkeypatch.setattr(module.httpx, "AsyncClient", lambda: client)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(dispatch(cast(Engine, sql), settings=EmailSettings("re_synthetic")), timeout=2)
        assert close_cancelled.is_set()

    asyncio.run(scenario())
    assert sql.closed == 2
    assert sql.completion == [{"id": IDENTIFIER, "provider": "offline-id", "success": True}]
