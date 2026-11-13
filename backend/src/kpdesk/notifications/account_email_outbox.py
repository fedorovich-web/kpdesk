"""Durable account-email dispatcher for SQL outbox + encrypted envelopes.

Uses the least-privilege auth Engine. Resend errors leave retryable SQL work.
The sender never prints recipient, token, ciphertext or API credentials.
"""
from __future__ import annotations

import sys
import uuid
from dataclasses import dataclass
from functools import partial
from urllib.parse import quote

import httpx
from cryptography.fernet import InvalidToken
from sqlalchemy import Engine, text

from kpdesk.async_work import run_thread
from kpdesk.db.account_lifecycle import _cipher
from kpdesk.notifications.resend_mail import (
    EmailDeliveryError,
    EmailSettings,
    close_owned_client,
    send_password_reset,
    send_registration_confirmation,
)


@dataclass(frozen=True, slots=True)
class QueuedEmail:
    challenge_id: uuid.UUID
    recipient: str
    purpose: str
    ciphertext: str


def _claim(engine: Engine) -> QueuedEmail | None:
    with engine.begin() as connection:
        connection.exec_driver_sql("SET LOCAL statement_timeout='10s'")
        connection.exec_driver_sql("SET LOCAL lock_timeout='5s'")
        record = connection.execute(text("""
            SELECT challenge_id,recipient_email,purpose,token_ciphertext
            FROM public.kpdesk_email_claim()
        """)).one_or_none()
    if record is None:
        return None
    return QueuedEmail(
        record.challenge_id, record.recipient_email,
        record.purpose, record.token_ciphertext,
    )


def _complete(engine: Engine, item: QueuedEmail, provider: str | None) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql("SET LOCAL statement_timeout='10s'")
        connection.exec_driver_sql("SET LOCAL lock_timeout='5s'")
        done = connection.scalar(text("""
            SELECT public.kpdesk_email_complete(:id,:provider,:success)
        """), {
            "id": item.challenge_id, "provider": provider,
            "success": provider is not None,
        })
        if done is not True:
            raise RuntimeError("Account email outbox lease expired or was lost.")


async def dispatch_one(
    engine: Engine,
    *,
    client: httpx.AsyncClient | None = None,
    settings: EmailSettings | None = None,
) -> str:
    """Return empty/sent/failed/unconfigured; never claim delivery on retry."""
    if settings is None:
        try:
            settings = EmailSettings.from_local_env()
        except (TypeError, ValueError):
            return "unconfigured"
    # Own the transaction until it settles; cancellation leaves the recoverable
    # SQL lease in place and must not start an email after a cancelled claim.
    item = await run_thread(partial(_claim, engine))
    if item is None:
        return "empty"
    provider_message_id: str | None = None
    try:
        plaintext = _cipher().decrypt(item.ciphertext.encode("ascii")).decode("ascii")
    except (InvalidToken, ValueError, UnicodeDecodeError) as error:
        # This is a key-rotation/data-integrity failure, not a provider error.
        # Do not mark sent; keep a recoverable lease for operator inspection.
        raise RuntimeError("Unable to decrypt account email envelope.") from error

    url_token = quote(plaintext, safe="")
    mail_key = "account-" + str(item.challenge_id)
    own_client = client is None
    transport = client if client is not None else httpx.AsyncClient()
    try:
        try:
            if item.purpose == "signup":
                provider_message_id = await send_registration_confirmation(
                    client=transport, settings=settings,
                    recipient=item.recipient,
                    verification_url="https://kpdesk.ru/auth/verify-email?token=" + url_token,
                    idempotency_key=mail_key,
                )
            elif item.purpose == "reset":
                provider_message_id = await send_password_reset(
                    client=transport, settings=settings,
                    recipient=item.recipient,
                    reset_url="https://kpdesk.ru/auth/reset-password?token=" + url_token,
                    idempotency_key=mail_key,
                )
            else:
                raise RuntimeError("Unsupported account email purpose.")
        except EmailDeliveryError:
            provider_message_id = None
        await run_thread(partial(_complete, engine, item, provider_message_id))
        return "sent" if provider_message_id is not None else "failed"
    finally:
        if own_client:
            await close_owned_client(transport, cause=sys.exception())
