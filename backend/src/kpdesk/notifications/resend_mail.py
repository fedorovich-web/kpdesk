"""Resend transport for opt-in registration confirmation mails.

The authenticated registration subsystem must generate, persist, rate-limit,
expire and consume a one-time verification token before invoking this adapter.
No unauthenticated HTTP endpoint is exposed by this module.
"""
from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from email.utils import parseaddr
from html import escape
from urllib.parse import parse_qs, urlsplit

import httpx
from dotenv import dotenv_values

from kpdesk.config import PROJECT_ROOT

RESEND_API_URL = "https://api.resend.com/emails"
SENDER = "KPDesk <no-replay@kpdesk.ru>"
SUPPORT_ADDRESS = "support@kpdesk.ru"
_VERIFICATION_PATH = "/auth/verify-email"
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._/-]{1,128}$")
_CLIENT_CLOSE_TIMEOUT_SECONDS = 10.0


async def close_owned_client(
    client: httpx.AsyncClient, *, cause: BaseException | None = None,
) -> None:
    """Settle bounded owned-client cleanup before propagating cancellation."""
    async def close() -> Exception | None:
        try:
            async with asyncio.timeout(_CLIENT_CLOSE_TIMEOUT_SECONDS):
                await client.aclose()
        except Exception as error:  # noqa: BLE001 - consumed and re-raised without orphan task logs
            return error
        return None

    task = asyncio.create_task(close())
    cancellation = cause if isinstance(cause, asyncio.CancelledError) else None
    while True:
        try:
            failure = await asyncio.shield(task)
            break
        except asyncio.CancelledError as error:
            if task.cancelled():
                raise
            cancellation = cancellation or error
    if cancellation is not None:
        raise cancellation from None
    if failure is not None:
        raise failure


@dataclass(frozen=True)
class EmailSettings:
    api_key: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.api_key.startswith("re_") or len(self.api_key) < 5:
            raise ValueError("Resend API key is not configured.")

    @classmethod
    def from_local_env(cls) -> EmailSettings:
        value = os.environ.get("KP_DESK_RESEND_API_KEY")
        if value is None:
            value = dotenv_values(PROJECT_ROOT / ".env").get("KP_DESK_RESEND_API_KEY")
        if not isinstance(value, str):
            raise TypeError("Resend API key is not configured.")
        return cls(api_key=value)


class EmailDeliveryError(Exception):
    def __init__(self, status_code: int | None = None) -> None:
        super().__init__("Transactional email provider rejected the request.")
        self.status_code = status_code


def _validate_verification_url(value: str) -> None:
    url = urlsplit(value)
    if (
        url.scheme != "https"
        or url.hostname != "kpdesk.ru"
        or url.port is not None
        or url.username is not None
        or url.password is not None
        or url.path != _VERIFICATION_PATH
        or url.fragment
    ):
        raise ValueError("Invalid verification link.")
    params = parse_qs(url.query, keep_blank_values=True)
    if set(params) != {"token"} or len(params["token"]) != 1 or not params["token"][0]:
        raise ValueError("Invalid verification link.")


async def send_registration_confirmation(
    *,
    client: httpx.AsyncClient,
    settings: EmailSettings,
    recipient: str,
    verification_url: str,
    idempotency_key: str,
) -> str:
    """Send only to validated recipient, returning provider message id.

    Caller must own a stored one-time token and stable event id. Delivery
    acknowledgement is NOT account verification or inbox delivery.
    """
    if (
        len(recipient) > 254 or not recipient or any(char.isspace() for char in recipient)
        or parseaddr(recipient) != ("", recipient) or "@" not in recipient
    ):
        raise ValueError("Invalid email recipient.")
    if not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
        raise ValueError("Invalid idempotency key.")
    _validate_verification_url(verification_url)
    safe_url = escape(verification_url, quote=True)
    payload = {
        "from": SENDER,
        "to": [recipient],
        "reply_to": SUPPORT_ADDRESS,
        "subject": "Подтверждение регистрации в KPDesk",
        "text": (
            "Здравствуйте!\n\nДля подтверждения адреса электронной почты "
            f"перейдите по ссылке:\n{verification_url}\n\n"
            "Если вы не регистрировались в KPDesk, проигнорируйте это письмо."
        ),
        "html": (
            '<!doctype html><html lang="ru"><body>'
            '<h1>Подтверждение регистрации в KPDesk</h1>'
            '<p>Чтобы подтвердить адрес электронной почты, перейдите по ссылке:</p>'
            f'<p><a href="{safe_url}">Подтвердить почту</a></p>'
            '<p>Если вы не регистрировались, проигнорируйте письмо.</p>'
            '</body></html>'
        ),
    }
    try:
        response = await client.post(
            RESEND_API_URL,
            json=payload,
            headers={
                "Authorization": f"Bearer {settings.api_key}",
                "Idempotency-Key": idempotency_key,
            },
            timeout=httpx.Timeout(10.0, connect=3.0),
        )
    except httpx.HTTPError:
        raise EmailDeliveryError() from None
    if response.status_code not in (200, 201):
        raise EmailDeliveryError(response.status_code)
    try:
        message_id = response.json()["id"]
    except (ValueError, TypeError, KeyError):
        raise EmailDeliveryError(response.status_code) from None
    if not isinstance(message_id, str) or not message_id:
        raise EmailDeliveryError(response.status_code)
    return message_id

async def send_password_reset(
    *,
    client: httpx.AsyncClient,
    settings: EmailSettings,
    recipient: str,
    reset_url: str,
    idempotency_key: str,
) -> str:
    """Send password-reset email with the same strict transport guarantees."""
    if (
        len(recipient) > 254 or not recipient or any(char.isspace() for char in recipient)
        or parseaddr(recipient) != ("", recipient) or "@" not in recipient
    ):
        raise ValueError("Invalid email recipient.")
    if not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
        raise ValueError("Invalid idempotency key.")
    url = urlsplit(reset_url)
    if (
        url.scheme != "https" or url.hostname != "kpdesk.ru"
        or url.port is not None or url.username is not None
        or url.password is not None or url.fragment
        or url.path != "/auth/reset-password"
    ):
        raise ValueError("Invalid password reset link.")
    params = parse_qs(url.query, keep_blank_values=True)
    if set(params) != {"token"} or len(params["token"]) != 1 or not params["token"][0]:
        raise ValueError("Invalid password reset link.")
    safe_url = escape(reset_url, quote=True)
    payload = {
        "from": SENDER,
        "to": [recipient],
        "reply_to": SUPPORT_ADDRESS,
        "subject": "Восстановление доступа к KPDesk",
        "text": (
            "Здравствуйте!\n\nЧтобы установить новый пароль, "
            f"перейдите по ссылке:\n{reset_url}\n\n"
            "Если вы не запрашивали восстановление, проигнорируйте письмо."
        ),
        "html": (
            '<!doctype html><html lang="ru"><body>'
            '<h1>Восстановление доступа к KPDesk</h1>'
            '<p>Чтобы установить новый пароль, перейдите по ссылке:</p>'
            f'<p><a href="{safe_url}">Установить новый пароль</a></p>'
            '<p>Если вы не запрашивали восстановление, проигнорируйте письмо.</p>'
            '</body></html>'
        ),
    }
    try:
        response = await client.post(
            RESEND_API_URL, json=payload,
            headers={
                "Authorization": f"Bearer {settings.api_key}",
                "Idempotency-Key": idempotency_key,
            },
            timeout=httpx.Timeout(10.0, connect=3.0),
        )
    except httpx.HTTPError:
        raise EmailDeliveryError() from None
    if response.status_code not in (200, 201):
        raise EmailDeliveryError(response.status_code)
    try:
        message_id = response.json()["id"]
    except (ValueError, KeyError, TypeError):
        raise EmailDeliveryError(response.status_code) from None
    if not isinstance(message_id, str) or not message_id:
        raise EmailDeliveryError(response.status_code)
    return message_id

async def send_team_invitation(
    *,
    client: httpx.AsyncClient,
    settings: EmailSettings,
    recipient: str,
    invitation_url: str,
    idempotency_key: str,
) -> str:
    """Send a one-time invitation without converting delivery into acceptance."""
    if (
        not recipient or len(recipient) > 254
        or any(char.isspace() for char in recipient)
        or parseaddr(recipient) != ("", recipient) or "@" not in recipient
    ):
        raise ValueError("Invalid invitation recipient.")
    if not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
        raise ValueError("Invalid invitation delivery idempotency key.")
    url = urlsplit(invitation_url)
    if (
        url.scheme != "https" or url.hostname != "kpdesk.ru"
        or url.path != "/auth/join" or url.port is not None
        or url.username is not None or url.password is not None or url.fragment
    ):
        raise ValueError("Invalid join URL.")
    params = parse_qs(url.query, keep_blank_values=True)
    if set(params) != {"token"} or len(params["token"]) != 1 or not re.fullmatch(
        r"[A-Za-z0-9_-]{43}", params["token"][0],
    ):
        raise ValueError("Invalid join token.")
    safe = escape(invitation_url, quote=True)
    payload = {
        "from": SENDER,
        "to": [recipient],
        "reply_to": SUPPORT_ADDRESS,
        "subject": "Приглашение в команду KPDesk",
        "text": (
            "Вас пригласили присоединиться к организации в KPDesk.\n"
            f"Чтобы создать аккаунт и принять приглашение, откройте:\n{invitation_url}\n\n"
            "Ссылка одноразовая. Если вы не ожидали приглашение, проигнорируйте письмо."
        ),
        "html": (
            '<!doctype html><html lang="ru"><body>'
            '<h1>Приглашение в команду KPDesk</h1>'
            '<p>Чтобы создать аккаунт и принять приглашение:</p>'
            f'<p><a href="{safe}">Присоединиться к команде</a></p>'
            '<p>Если приглашение не запрашивали, проигнорируйте письмо.</p>'
            '</body></html>'
        ),
    }
    try:
        response = await client.post(
            RESEND_API_URL, json=payload,
            headers={
                "Authorization": f"Bearer {settings.api_key}",
                "Idempotency-Key": idempotency_key,
            },
            timeout=httpx.Timeout(10.0, connect=3.0),
        )
    except httpx.HTTPError:
        raise EmailDeliveryError() from None
    if response.status_code not in (200, 201):
        raise EmailDeliveryError(response.status_code)
    try:
        message_id = response.json()["id"]
    except (ValueError, TypeError, KeyError):
        raise EmailDeliveryError(response.status_code) from None
    if not isinstance(message_id, str) or not message_id:
        raise EmailDeliveryError(response.status_code)
    return message_id
