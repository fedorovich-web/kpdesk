"""Account copy keeps privacy and failure contracts; no DB or mail delivery."""
from unittest.mock import Mock

import pytest
from fastapi import BackgroundTasks, Request, Response
from sqlalchemy import Engine

from kpdesk.api import account_routes, tenant_session_routes
from kpdesk.api.errors import ApiError
from kpdesk.db.auth_sessions import LoginDenied


def request() -> Request:
    return Request({'type': 'http', 'client': ('127.0.0.1', 12345), 'headers': []})


@pytest.mark.parametrize('operation', ['register', 'resend', 'forgot'])
@pytest.mark.parametrize('queued', [False, True])
def test_generic_account_response_never_reveals_account_state_or_sends_mail(
    monkeypatch: pytest.MonkeyPatch, operation: str, queued: bool,
) -> None:
    lifecycle = Mock()
    lifecycle.register.return_value = queued
    lifecycle.resend_confirmation.return_value = queued
    lifecycle.request_password_reset.return_value = queued
    monkeypatch.setattr(account_routes, 'AccountLifecycle', lambda engine: lifecycle)
    engine, response, background = Mock(spec=Engine), Response(), BackgroundTasks()

    async def no_delivery(_: Engine) -> str:
        raise AssertionError('Copy contract test must not dispatch an email')

    if operation == 'register':
        result = account_routes.register_account(account_routes.RegisterRequest(
            email='synthetic@example.test', password='synthetic-password-123',
            organization_name='Тестовая компания',
        ), request(), response, background, engine, no_delivery)
    elif operation == 'resend':
        result = account_routes.resend_confirmation(account_routes.ResendConfirmationRequest(
            email='synthetic@example.test', password='synthetic-password-123',
        ), request(), response, background, engine, no_delivery)
    else:
        result = account_routes.forgot_password(account_routes.ForgotPasswordRequest(
            email='synthetic@example.test',
        ), request(), response, background, engine, no_delivery)
    assert result == {'status': 'accepted'}
    assert response.headers['Cache-Control'] == 'no-store'
    assert len(background.tasks) == int(queued)
    engine.begin.assert_not_called()


def test_login_failures_keep_one_neutral_message_for_private_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = set()
    gateway = Mock()
    monkeypatch.setattr(tenant_session_routes, 'CommittedLogin', lambda engine: gateway)
    engine = Mock(spec=Engine)
    for reason in ('missing-account-sentinel', 'wrong-password-sentinel', 'locked-account-sentinel'):
        gateway.attempt.side_effect = LoginDenied(reason)
        with pytest.raises(ApiError) as caught:
            tenant_session_routes.login(tenant_session_routes.LoginRequest(
                email='synthetic@example.test', password='synthetic-password-123',
            ), request(), Response(), engine)
        assert caught.value.status_code == 401
        assert caught.value.code == 'invalid_login'
        assert reason not in caught.value.message
        messages.add(caught.value.message)
    assert len(messages) == 1
    assert any('а' <= letter.lower() <= 'я' for letter in messages.pop())
    engine.begin.assert_not_called()


@pytest.mark.parametrize('purpose', ['verify', 'reset'])
def test_invalid_links_keep_the_original_code_without_echoing_the_token(
    monkeypatch: pytest.MonkeyPatch, purpose: str,
) -> None:
    lifecycle = Mock()
    lifecycle.verify_email.return_value = None
    lifecycle.reset_password.return_value = False
    monkeypatch.setattr(account_routes, 'AccountLifecycle', lambda engine: lifecycle)
    engine = Mock(spec=Engine)
    token = 'a' * 43
    with pytest.raises(ApiError) as caught:
        if purpose == 'verify':
            account_routes.verify_email(account_routes.TokenRequest(token=token), Response(), engine)
        else:
            account_routes.reset_password(account_routes.ResetPasswordRequest(
                token=token, password='synthetic-password-123',
            ), Response(), engine)
    assert caught.value.status_code == 400
    assert caught.value.code == ('invalid_email_token' if purpose == 'verify' else 'invalid_reset_token')
    assert token not in caught.value.message
    engine.begin.assert_not_called()
