"""Offline Lava contract tests; all HTTP uses an in-memory transport."""
import json
from collections.abc import AsyncIterator
from dataclasses import FrozenInstanceError
from uuid import UUID

import httpx
import pytest

from kpdesk.billing.lava import LavaClient, LavaError
from kpdesk.billing.plans import PLANS
from kpdesk.billing.settings import BillingSettings

PRODUCT = UUID("d62d916c-f588-4af8-934e-b0945e6e9531")
OFFER = UUID("b997b67b-f29b-40d4-a085-72faf5add72b")
INVOICE = UUID("9c5f2055-100a-4e3c-b4ba-a7e3a66b81ee")
ORDER = UUID("b8a2185c-32c3-4ab0-97f7-439b09042c20")


def environment() -> dict[str, str]:
    env = {"KP_DESK_LAVA_ENABLED": "true", "KP_DESK_LAVA_API_KEY": "test-api-key",
           "KP_DESK_LAVA_WEBHOOK_SECRET": "w" * 48}
    for plan in PLANS:
        env[f"KP_DESK_LAVA_{plan.upper()}_PRODUCT_ID"] = str(PRODUCT)
        env[f"KP_DESK_LAVA_{plan.upper()}_OFFER_ID"] = str(OFFER)
    return env


def settings() -> BillingSettings:
    return BillingSettings.from_env(environment())


def readback(*, amount: str = "299.00", status: str = "COMPLETED") -> str:
    return json.dumps({"id": str(INVOICE), "status": status,
                       "receipt": {"currency": "RUB"}, "buyer": {"email": "buyer@example.test"},
                       "product": {"name": "KPDesk", "offer": "30 days"},
                       "clientUtm": {"utm_content": str(ORDER)}}).replace(
                           '"currency": "RUB"', f'"currency": "RUB", "amount": {amount}')


def test_plans_and_settings_are_immutable_and_secret_safe() -> None:
    assert [(p.price_minor, p.people_limit, p.new_quotes, p.catalog_items, p.days)
            for p in PLANS.values()] == [(29900, 1, 30, 500, 30),
                                       (49900, 5, 100, 2000, 30), (99000, 15, 300, 5000, 30)]
    configured = settings()
    assert configured.offers["start"].product_id == PRODUCT
    assert configured.public_origin == "https://kpdesk.ru"
    assert "test-api-key" not in repr(configured) and "w" * 48 not in repr(configured)
    with pytest.raises(FrozenInstanceError):
        PLANS["start"].price_minor = 1  # type: ignore[misc]
    with pytest.raises(TypeError):
        configured.offers["start"] = configured.offers["team"]  # type: ignore[index]
    assert not BillingSettings.from_env({}).enabled
    assert not BillingSettings.from_env({"KP_DESK_LAVA_ENABLED": "false"}).enabled


@pytest.mark.parametrize("value", ["", "1", "TRUE", " true", "False", "yes"])
def test_enabled_flag_is_strict(value: str) -> None:
    with pytest.raises(ValueError):
        BillingSettings.from_env({"KP_DESK_LAVA_ENABLED": value})


@pytest.mark.parametrize("key,value", [
    ("KP_DESK_LAVA_API_KEY", ""), ("KP_DESK_LAVA_API_KEY", "key\n"),
    ("KP_DESK_LAVA_WEBHOOK_SECRET", "x" * 31),
    ("KP_DESK_LAVA_WEBHOOK_SECRET", "x" * 81),
    ("KP_DESK_LAVA_WEBHOOK_SECRET", "ю" * 48),
    ("KP_DESK_LAVA_START_PRODUCT_ID", "not-a-uuid"),
    ("KP_DESK_LAVA_START_OFFER_ID", ""),
])
def test_enabled_settings_fail_closed(key: str, value: str) -> None:
    env = environment()
    env[key] = value
    with pytest.raises(ValueError) as error:
        BillingSettings.from_env(env)
    assert "test-api-key" not in str(error.value)


def test_webhook_secret_must_differ_from_provider_key() -> None:
    env = environment()
    env["KP_DESK_LAVA_API_KEY"] = env["KP_DESK_LAVA_WEBHOOK_SECRET"]
    with pytest.raises(ValueError):
        BillingSettings.from_env(env)


@pytest.mark.asyncio
async def test_disabled_provider_does_not_send() -> None:
    def reject(request: httpx.Request) -> httpx.Response:
        pytest.fail("Disabled billing attempted HTTP")
    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as client:
        provider = LavaClient(BillingSettings.from_env({}), client)
        with pytest.raises(LavaError, match="billing_disabled"):
            await provider.create_invoice("buyer@example.test", OFFER, 29900, ORDER)
        with pytest.raises(LavaError, match="billing_disabled"):
            await provider.verify_invoice(INVOICE)


@pytest.mark.asyncio
async def test_checkout_fixed_request_and_canonical_returns() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "https://gate.lava.top/api/v3/invoice"
        assert request.headers["X-Api-Key"] == "test-api-key"
        data = json.loads(request.content)
        assert data == {
            "email": "buyer@example.test", "offerId": str(OFFER), "amount": 299,
            "currency": "RUB", "periodicity": "ONE_TIME", "buyerLanguage": "RU",
            "clientUtm": {"utm_source": "kpdesk", "utm_content": str(ORDER)},
            "successful_return_url": f"https://kpdesk.ru/app?billing_order={ORDER}&billing_return=success#workspace-billing",
            "failure_return_url": f"https://kpdesk.ru/app?billing_order={ORDER}&billing_return=failure#workspace-billing",
            "cancel_return_url": f"https://kpdesk.ru/app?billing_order={ORDER}&billing_return=cancel#workspace-billing",
        }
        return httpx.Response(201, json={"id": str(INVOICE), "paymentUrl": "https://pay.lava.top/invoice/a"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await LavaClient(settings(), client).create_invoice(
            "buyer@example.test", OFFER, 29900, ORDER)
    assert result.invoice_id == INVOICE
    assert result.payment_url == "https://pay.lava.top/invoice/a"


@pytest.mark.parametrize("url", ["http://pay.lava.top/a", "https://pay.lava.top.evil.test/a",
    "https://user@lava.top/a", "https://lava.top:8443/a", "//lava.top/a",
    "https://evil.test/a", "https://lava.top\\@evil.test/a", "https://lava.top/a\n"])
@pytest.mark.asyncio
async def test_unsafe_payment_urls_are_ambiguous(url: str) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
            200, json={"id": str(INVOICE), "paymentUrl": url}))) as client:
        with pytest.raises(LavaError) as error:
            await LavaClient(settings(), client).create_invoice("buyer@example.test", OFFER, 29900, ORDER)
    assert error.value.ambiguous and error.value.code == "provider_invalid_response"


@pytest.mark.parametrize("status,ambiguous", [(400, False), (401, False), (429, False),
    (302, True), (500, True), (503, True)])
@pytest.mark.asyncio
async def test_errors_are_safe_no_retries_or_redirects(status: int, ambiguous: bool) -> None:
    requests = []
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, headers={"Location": "https://evil.test"},
                              json={"error": "buyer@example.test test-api-key"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=True) as client:
        with pytest.raises(LavaError) as error:
            await LavaClient(settings(), client).create_invoice("buyer@example.test", OFFER, 29900, ORDER)
    assert len(requests) == 1
    assert error.value.ambiguous == ambiguous
    assert "buyer" not in str(error.value) and "test-api-key" not in str(error.value)


@pytest.mark.asyncio
async def test_timeout_is_ambiguous_and_not_retried() -> None:
    requests = []
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ReadTimeout("buyer@example.test test-api-key", request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(LavaError) as error:
            await LavaClient(settings(), client).create_invoice("buyer@example.test", OFFER, 29900, ORDER)
    assert len(requests) == 1 and error.value.ambiguous
    assert "buyer" not in str(error.value)


@pytest.mark.parametrize("status,expected", [("NEW", "new"), ("IN_PROGRESS", "in_progress"),
                                           ("COMPLETED", "completed"), ("FAILED", "failed")])
@pytest.mark.asyncio
async def test_verified_readback_official_shape(status: str, expected: str) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == f"https://gate.lava.top/api/v1/invoices/{INVOICE}"
        return httpx.Response(200, text=readback(status=status))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await LavaClient(settings(), client).verify_invoice(INVOICE)
    assert (result.invoice_id, result.amount_minor, result.currency, result.email,
            result.correlation, result.status) == (INVOICE, 29900, "RUB", "buyer@example.test", ORDER, expected)
    assert result.product_id is None and result.offer_id is None


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-1", "299.001", "true", '"299.00"',
                                    "1e999999999", "0e999999999"])
@pytest.mark.asyncio
async def test_invalid_money_is_not_settled(amount: str) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
            200, text=readback(amount=amount)))) as client:
        with pytest.raises(LavaError) as error:
            await LavaClient(settings(), client).verify_invoice(INVOICE)
    assert error.value.code == "provider_invalid_response" and not error.value.ambiguous


@pytest.mark.asyncio
async def test_decimal_readback_does_not_round_through_float() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
            200, text=readback(amount="90071992547409.91")))) as client:
        result = await LavaClient(settings(), client).verify_invoice(INVOICE)
    assert result.amount_minor == 9007199254740991


@pytest.mark.parametrize("replacement", [
    {"id": str(ORDER)}, {"clientUtm": {"utm_content": "bad"}}, {"status": "PAID"},
    {"buyer": {}}, {"receipt": {"amount": 299, "currency": "XXX"}},
])
@pytest.mark.asyncio
async def test_malformed_or_wrong_invoice_readback_fails_closed(replacement: dict[str, object]) -> None:
    payload = json.loads(readback())
    payload.update(replacement)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
            200, json=payload))) as client:
        with pytest.raises(LavaError):
            await LavaClient(settings(), client).verify_invoice(INVOICE)


@pytest.mark.parametrize("url", ["https://lava.top/pay/a", "https://app.lava.top/pay/a",
                                 "https://pay.lava.top:443/a"])
@pytest.mark.asyncio
async def test_all_supported_payment_hosts(url: str) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
            200, json={"id": str(INVOICE), "paymentUrl": url}))) as client:
        result = await LavaClient(settings(), client).create_invoice(
            "buyer@example.test", OFFER, 29900, ORDER)
    assert result.payment_url == url


@pytest.mark.parametrize("amount", [True, 0, -100, 29901])
@pytest.mark.asyncio
async def test_invalid_checkout_amount_never_sends(amount: int) -> None:
    def reject(request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid invoice attempted HTTP")
    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as client:
        with pytest.raises(LavaError, match="invoice_request_invalid"):
            await LavaClient(settings(), client).create_invoice("buyer@example.test", OFFER, amount, ORDER)


@pytest.mark.parametrize("payload", ['{"id":"bad","paymentUrl":"https://lava.top"}',
                                     "null", "not json", "[]", "{}"])
@pytest.mark.asyncio
async def test_invalid_creation_response_is_always_ambiguous(payload: str) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
            200, text=payload))) as client:
        with pytest.raises(LavaError) as error:
            await LavaClient(settings(), client).create_invoice("buyer@example.test", OFFER, 29900, ORDER)
    assert error.value.ambiguous and error.value.code == "provider_invalid_response"


@pytest.mark.parametrize("method", ["POST", "GET"])
@pytest.mark.asyncio
async def test_deep_provider_json_is_normalized_without_retry(method: str) -> None:
    # Below the response byte cap but beyond the JSON decoder's recursion bound.
    payload = '{"nested":' + '[' * 30000 + '0' + ']' * 30000 + '}'
    assert len(payload.encode()) < 65536
    with pytest.raises(RecursionError):
        json.loads(payload)
    requests = []
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=payload)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = LavaClient(settings(), client)
        with pytest.raises(LavaError) as error:
            if method == "POST":
                await provider.create_invoice("buyer@example.test", OFFER, 29900, ORDER)
            else:
                await provider.verify_invoice(INVOICE)
    assert len(requests) == 1
    assert error.value.code == "provider_invalid_response"
    assert error.value.ambiguous == (method == "POST")


class TrackedResponseStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], *, fail_after_chunks: bool = False) -> None:
        self.chunks = chunks
        self.fail_after_chunks = fail_after_chunks
        self.read_chunks = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.read_chunks += 1
            yield chunk
        if self.fail_after_chunks:
            raise httpx.ReadError("private-provider-detail")

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize("method", ["POST", "GET"])
@pytest.mark.parametrize("owned_client", [False, True])
@pytest.mark.asyncio
async def test_response_limit_stops_reading_and_closes_stream(
    method: str, owned_client: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = TrackedResponseStream([b" " * 65536, b"x", b"must-not-read"])
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        # A lying length must not bypass the streamed response limit.
        return httpx.Response(200, headers={"Content-Length": "2"}, stream=stream)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        if owned_client:
            monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)
        provider = LavaClient(settings(), None if owned_client else client)
        with pytest.raises(LavaError) as error:
            if method == "POST":
                await provider.create_invoice("buyer@example.test", OFFER, 29900, ORDER)
            else:
                await provider.verify_invoice(INVOICE)
        assert client.is_closed == owned_client
    finally:
        await client.aclose()
    assert len(requests) == 1
    assert stream.read_chunks == 2
    assert stream.closed
    assert error.value.code == "provider_invalid_response"
    assert error.value.ambiguous == (method == "POST")


@pytest.mark.parametrize("status", [302, 400, 429, 503])
@pytest.mark.asyncio
async def test_rejected_response_body_is_not_read(status: int) -> None:
    stream = TrackedResponseStream([b"private-provider-detail"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(status, stream=stream),
    )) as client:
        with pytest.raises(LavaError) as error:
            await LavaClient(settings(), client).create_invoice(
                "buyer@example.test", OFFER, 29900, ORDER)
    assert stream.read_chunks == 0 and stream.closed
    assert error.value.code == ("provider_rejected" if 400 <= status < 500
                                else "provider_unavailable")
    assert error.value.ambiguous == (not 400 <= status < 500)


@pytest.mark.asyncio
async def test_response_exactly_at_limit_is_accepted() -> None:
    payload = readback().encode()
    stream = TrackedResponseStream([payload, b" " * (65536 - len(payload))])
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, stream=stream),
    )) as client:
        result = await LavaClient(settings(), client).verify_invoice(INVOICE)
    assert result.amount_minor == 29900 and stream.closed


@pytest.mark.parametrize("method", ["POST", "GET"])
@pytest.mark.asyncio
async def test_interrupted_response_stream_is_closed_and_never_retried(method: str) -> None:
    stream = TrackedResponseStream([b'{"id":'], fail_after_chunks=True)
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(LavaError) as error:
            provider = LavaClient(settings(), client)
            if method == "POST":
                await provider.create_invoice("buyer@example.test", OFFER, 29900, ORDER)
            else:
                await provider.verify_invoice(INVOICE)
    assert len(requests) == 1 and stream.closed
    assert error.value.code == "provider_unavailable"
    assert error.value.ambiguous == (method == "POST")


@pytest.mark.parametrize("method", ["POST", "GET"])
@pytest.mark.asyncio
async def test_unexpected_compression_is_rejected_before_body_or_decoder(method: str) -> None:
    import gzip

    stream = TrackedResponseStream([gzip.compress(b" " * 1024 * 1024)])
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(LavaError) as error:
            provider = LavaClient(settings(), client)
            if method == "POST":
                await provider.create_invoice("buyer@example.test", OFFER, 29900, ORDER)
            else:
                await provider.verify_invoice(INVOICE)
    assert stream.read_chunks == 0 and stream.closed
    assert len(requests) == 1 and requests[0].headers["Accept-Encoding"] == "identity"
    assert error.value.code == "provider_invalid_response"
    assert error.value.ambiguous == (method == "POST")
