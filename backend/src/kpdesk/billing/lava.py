"""Lava HTTP boundary: no retries, trusted endpoints, strict readback parsing."""
import json
from contextlib import AsyncExitStack
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from typing import Literal, cast
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from kpdesk.billing.settings import BillingSettings

InvoiceStatus = Literal["new", "in_progress", "completed", "failed"]
_STATUSES = {"NEW": "new", "IN_PROGRESS": "in_progress",
             "COMPLETED": "completed", "FAILED": "failed"}


class LavaError(Exception):
    """Only stable, non-sensitive codes cross the provider boundary."""

    def __init__(self, code: str, *, ambiguous: bool = False) -> None:
        self.code = code
        self.ambiguous = ambiguous
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class InvoiceResult:
    invoice_id: UUID
    payment_url: str


@dataclass(frozen=True, slots=True)
class VerifiedInvoice:
    invoice_id: UUID
    status: InvoiceStatus
    amount_minor: int
    currency: str
    email: str
    correlation: UUID
    # InvoiceResponseV2 contains product/offer names, never their identifiers.
    product_id: UUID | None = None
    offer_id: UUID | None = None


def _uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise TypeError
    return UUID(value)


def _payment_url(value: object) -> str:
    if (not isinstance(value, str) or not value or "\\" in value
            or any(ord(c) <= 32 or ord(c) >= 127 for c in value)):
        raise ValueError
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname not in {
        "lava.top", "app.lava.top", "pay.lava.top"
    } or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443)):
        raise ValueError
    return value


def _money(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise TypeError
    amount = Decimal(value)
    exponent = amount.as_tuple().exponent
    if (not amount.is_finite() or amount < 0 or not isinstance(exponent, int)
            or not -2 <= exponent <= 16 or amount > Decimal("92233720368547758.07")):
        raise ValueError
    # Bound numerical work for untrusted provider responses; bigint SQL storage.
    with localcontext() as context:
        context.prec = 30
        return int(amount * 100)


class LavaClient:
    def __init__(self, settings: BillingSettings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client

    def _check_enabled(self) -> None:
        if not self.settings.enabled:
            raise LavaError("billing_disabled")

    async def _request(
        self, method: str, path: str, *, payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self._check_enabled()
        creating = method == "POST"
        try:
            async with AsyncExitStack() as stack:
                client = self._client
                if client is None:
                    client = await stack.enter_async_context(httpx.AsyncClient(trust_env=False))
                response = await stack.enter_async_context(client.stream(
                    method, "https://gate.lava.top" + path, json=payload,
                    headers={"X-Api-Key": self.settings.api_key, "Accept-Encoding": "identity"},
                    timeout=15.0, follow_redirects=False))
                if not 200 <= response.status_code < 300:
                    raise LavaError("provider_rejected" if 400 <= response.status_code < 500
                                    else "provider_unavailable",
                                    ambiguous=creating and not 400 <= response.status_code < 500)
                # Reject compression before invoking any decoder: its output can
                # exceed the limit within a single compressed transport chunk.
                if response.headers.get('Content-Encoding', '').strip().lower() not in ('', 'identity'):
                    raise LavaError("provider_invalid_response", ambiguous=creating)
                if response.is_stream_consumed:
                    # In-memory transports may return an already buffered response.
                    content = bytearray(response.content[:65537])
                    if len(response.content) > 65536:
                        raise LavaError("provider_invalid_response", ambiguous=creating)
                else:
                    content = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(content) + len(chunk) > 65536:
                            raise LavaError("provider_invalid_response", ambiguous=creating)
                        content.extend(chunk)
        except httpx.RequestError:
            raise LavaError("provider_unavailable", ambiguous=creating) from None
        try:
            data: object = json.loads(content, parse_float=Decimal)
            if not isinstance(data, dict):
                raise TypeError
            return data
        except (ValueError, TypeError, UnicodeError, InvalidOperation, RecursionError):
            raise LavaError("provider_invalid_response", ambiguous=creating) from None

    async def create_invoice(
        self, email: str, offer_id: UUID, amount_minor: int, correlation: UUID,
    ) -> InvoiceResult:
        self._check_enabled()
        if (not isinstance(amount_minor, int) or isinstance(amount_minor, bool)
                or amount_minor <= 0 or amount_minor % 100
                or not isinstance(correlation, UUID) or not isinstance(offer_id, UUID)
                or not isinstance(email, str) or not email or len(email) > 320
                or any(ord(c) < 32 for c in email)
                or offer_id not in {offer.offer_id for offer in self.settings.offers.values()}):
            raise LavaError("invoice_request_invalid")
        def return_url(outcome: str) -> str:
            return (f"{self.settings.public_origin}/app?billing_order={correlation}"
                    f"&billing_return={outcome}#workspace-billing")
        data = await self._request("POST", "/api/v3/invoice", payload={
            "email": email, "offerId": str(offer_id), "amount": amount_minor // 100,
            "currency": "RUB", "periodicity": "ONE_TIME", "buyerLanguage": "RU",
            "clientUtm": {"utm_source": "kpdesk", "utm_content": str(correlation)},
            "successful_return_url": return_url("success"),
            "failure_return_url": return_url("failure"),
            "cancel_return_url": return_url("cancel"),
        })
        try:
            return InvoiceResult(_uuid(data["id"]), _payment_url(data["paymentUrl"]))
        except (ValueError, TypeError, KeyError):
            raise LavaError("provider_invalid_response", ambiguous=True) from None

    async def verify_invoice(self, invoice_id: UUID) -> VerifiedInvoice:
        self._check_enabled()
        if not isinstance(invoice_id, UUID):
            raise LavaError("invoice_request_invalid")
        data = await self._request("GET", f"/api/v1/invoices/{invoice_id}")
        try:
            returned_id = _uuid(data["id"])
            receipt, buyer, utm = data["receipt"], data["buyer"], data["clientUtm"]
            if (returned_id != invoice_id or not isinstance(receipt, dict)
                    or not isinstance(buyer, dict) or not isinstance(utm, dict)):
                raise ValueError
            currency, email = receipt["currency"], buyer["email"]
            if (currency not in ("RUB", "USD", "EUR") or not isinstance(email, str)
                    or not email or len(email) > 320 or any(ord(c) < 32 for c in email)):
                raise ValueError
            status = _STATUSES[str(data["status"])]
            return VerifiedInvoice(returned_id, cast(InvoiceStatus, status),
                                   _money(receipt["amount"]), cast(str, currency), email,
                                   _uuid(utm["utm_content"]))
        except (ValueError, TypeError, KeyError, InvalidOperation, OverflowError):
            raise LavaError("provider_invalid_response") from None
