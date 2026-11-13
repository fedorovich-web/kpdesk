"""Resend contract tests: synthetic address, no live messages or credentials."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from kpdesk.notifications import resend_mail
from kpdesk.notifications.resend_mail import (
    EmailDeliveryError,
    EmailSettings,
    send_registration_confirmation,
)


def test_resend_verification_message_is_scoped_and_idempotent() -> None:
    captured: list[httpx.Request] = []

    def fake_resend(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"id": "test-email-id"})

    async def scenario() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake_resend)) as client:
            return await send_registration_confirmation(
                client=client,
                settings=EmailSettings(api_key="re_testing_local_only"),
                recipient="tester@example.org",
                verification_url="https://kpdesk.ru/auth/verify-email?token=example",
                idempotency_key="confirm_test_registration",
            )

    assert asyncio.run(scenario()) == "test-email-id"
    assert len(captured) == 1
    request = captured[0]
    assert str(request.url) == "https://api.resend.com/emails"
    assert request.headers["Authorization"] == "Bearer re_testing_local_only"
    assert request.headers["Idempotency-Key"] == "confirm_test_registration"
    content = json.loads(request.content)
    assert content["from"] == "KPDesk <no-replay@kpdesk.ru>"
    assert content["reply_to"] == "support@kpdesk.ru"
    assert content["to"] == ["tester@example.org"]
    assert content["text"].count("https://kpdesk.ru/auth/verify-email?token=example") == 1
    assert "https://kpdesk.ru/auth/verify-email?token=example" in content["html"]


def test_registration_plaintext_has_readable_paragraphs_and_standalone_link() -> None:
    messages: list[dict[str, object]] = []

    def capture(request: httpx.Request) -> httpx.Response:
        messages.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "offline-registration"})

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(capture)) as client:
            await send_registration_confirmation(
                client=client, settings=EmailSettings(api_key="re_synthetic"),
                recipient="tester@example.org",
                verification_url="https://kpdesk.ru/auth/verify-email?token=example",
                idempotency_key="registration-paragraphs",
            )

    asyncio.run(scenario())
    assert len(messages) == 1
    plaintext = messages[0]["text"]
    assert isinstance(plaintext, str)
    lines = plaintext.splitlines()
    assert len(lines) == 6
    assert lines[1] == lines[4] == ""
    assert lines[3] == "https://kpdesk.ru/auth/verify-email?token=example"
    assert "\\n" not in plaintext


@pytest.mark.parametrize("url", [
    "http://kpdesk.ru/auth/verify-email?token=abc",
    "https://other.example/auth/verify-email?token=abc",
    "https://kpdesk.ru.evil.example/auth/verify-email?token=abc",
    "https://kpdesk.ru/auth/verify-email?token=",
    "https://kpdesk.ru/auth/verify-email?token=abc#fragment",
])
def test_untrusted_verification_link_never_reaches_provider(url: str) -> None:
    calls: list[httpx.Request] = []

    def fake(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"id": "test"})

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as client:
            await send_registration_confirmation(
                client=client, settings=EmailSettings(api_key="re_testing_local_only"),
                recipient="tester@example.org", verification_url=url,
                idempotency_key="confirm_1",
            )

    with pytest.raises(ValueError):
        asyncio.run(scenario())
    assert calls == []


def test_provider_failure_is_sanitized() -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(429, json={"message": "re_sensitive_dont_print"})
        )
        async with httpx.AsyncClient(transport=transport) as client:
            await send_registration_confirmation(
                client=client, settings=EmailSettings(api_key="re_testing_local_only"),
                recipient="tester@example.org",
                verification_url="https://kpdesk.ru/auth/verify-email?token=abc",
                idempotency_key="confirm_2",
            )

    with pytest.raises(EmailDeliveryError) as error:
        asyncio.run(scenario())
    assert error.value.status_code == 429
    assert "re_sensitive_dont_print" not in str(error.value)


def test_no_api_key_is_fail_closed() -> None:
    with pytest.raises(ValueError):
        EmailSettings(api_key="")


def test_empty_process_key_disables_synthetic_file_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(resend_mail, "PROJECT_ROOT", tmp_path)
    (tmp_path / ".env").write_text("KP_DESK_RESEND_API_KEY=re_synthetic_file\n", encoding="utf-8")
    monkeypatch.setenv("KP_DESK_RESEND_API_KEY", "")
    with pytest.raises(ValueError, match="not configured"):
        EmailSettings.from_local_env()


def test_process_key_takes_precedence_over_synthetic_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(resend_mail, "PROJECT_ROOT", tmp_path)
    (tmp_path / ".env").write_text("KP_DESK_RESEND_API_KEY=re_synthetic_file\n", encoding="utf-8")
    monkeypatch.setenv("KP_DESK_RESEND_API_KEY", "re_synthetic_process")
    assert EmailSettings.from_local_env().api_key == "re_synthetic_process"


def test_missing_process_key_uses_synthetic_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(resend_mail, "PROJECT_ROOT", tmp_path)
    (tmp_path / ".env").write_text("KP_DESK_RESEND_API_KEY=re_synthetic_file\n", encoding="utf-8")
    monkeypatch.delenv("KP_DESK_RESEND_API_KEY", raising=False)
    assert EmailSettings.from_local_env().api_key == "re_synthetic_file"
