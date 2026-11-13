"""Durable transactional dispatcher for unregistered-team invitation emails."""
from __future__ import annotations

import sys
import uuid
from dataclasses import dataclass
from functools import partial
from urllib.parse import quote

import httpx
from sqlalchemy import Engine, text

from kpdesk.async_work import run_thread
from kpdesk.db.account_lifecycle import _cipher
from kpdesk.notifications.resend_mail import (
    EmailDeliveryError,
    EmailSettings,
    close_owned_client,
    send_team_invitation,
)


@dataclass(frozen=True, slots=True)
class QueuedInvitation:
    invite_id: uuid.UUID
    recipient: str
    ciphertext: str


def _claim(engine: Engine) -> QueuedInvitation | None:
    with engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL statement_timeout='10s'")
        conn.exec_driver_sql("SET LOCAL lock_timeout='5s'")
        row = conn.execute(text("""
            SELECT invite_id,recipient,ciphertext
            FROM public.kpdesk_email_invite_claim()
        """)).one_or_none()
    if row is None:
        return None
    return QueuedInvitation(row.invite_id, row.recipient, row.ciphertext)


def _complete(engine: Engine, invite_id: uuid.UUID, message_id: str | None) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL statement_timeout='10s'")
        conn.exec_driver_sql("SET LOCAL lock_timeout='5s'")
        acknowledged = conn.scalar(text("""
            SELECT public.kpdesk_email_invite_complete(
                :id,:provider,:success
            )
        """), {
            "id": invite_id, "provider": message_id,
            "success": message_id is not None,
        })
        if acknowledged is not True:
            raise RuntimeError("Team invite outbox lease expired before acknowledge.")


async def dispatch_team_one(
    engine: Engine, *, client: httpx.AsyncClient | None = None,
    settings: EmailSettings | None = None,
) -> str:
    if settings is None:
        try:
            settings = EmailSettings.from_local_env()
        except (TypeError, ValueError):
            return "unconfigured"
    # Cancellation must settle the claim without initiating a provider send.
    row = await run_thread(partial(_claim, engine))
    if row is None:
        return "empty"
    invite_id: uuid.UUID = row.invite_id
    # Key loss is fatal; do not acknowledge or fabricate a sent message.
    token = _cipher().decrypt(row.ciphertext.encode("ascii")).decode("ascii")
    owned = client is None
    transport = client if client is not None else httpx.AsyncClient()
    message_id: str | None = None
    try:
        try:
            message_id = await send_team_invitation(
                client=transport, settings=settings,
                recipient=row.recipient,
                invitation_url="https://kpdesk.ru/auth/join?token=" + quote(token, safe=""),
                idempotency_key="invite-" + str(invite_id),
            )
        except EmailDeliveryError:
            message_id = None
        await run_thread(partial(_complete, engine, invite_id, message_id))
        return "sent" if message_id is not None else "failed"
    finally:
        if owned:
            await close_owned_client(transport, cause=sys.exception())
