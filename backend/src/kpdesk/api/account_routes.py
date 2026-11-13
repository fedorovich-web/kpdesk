"""Account registration, email verification and password recovery.

These routes are mounted ONLY in the opt-in HTTPS tenant application.
They never expose verification tokens and never insert accounts as DB owner.
"""
from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine

from kpdesk.api.errors import ApiError
from kpdesk.api.tenant_session_routes import get_unreleased_login_engine
from kpdesk.db.account_lifecycle import AccountLifecycle
from kpdesk.db.auth_sessions import LoginDenied
from kpdesk.db.team_email_invites import register_invited
from kpdesk.notifications.account_email_outbox import dispatch_one

router = APIRouter(prefix="/api/session")

type AccountEmailDispatcher = Callable[[Engine], Awaitable[str]]


def get_account_email_dispatcher() -> AccountEmailDispatcher:
    return dispatch_one


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=12, max_length=256)
    organization_name: str = Field(min_length=1, max_length=200)


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    token: str = Field(min_length=43, max_length=43)


class VerifiedAccountResponse(BaseModel):
    user_id: uuid.UUID
    organization_id: uuid.UUID


class JoinInvitationRequest(TokenRequest):
    password: str = Field(min_length=12, max_length=256)


class ForgotPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    email: str = Field(min_length=1, max_length=254)


class ResetPasswordRequest(TokenRequest):
    password: str = Field(min_length=12, max_length=256)


def _peer(request: Request) -> str:
    if request.client is None:
        raise ApiError(400, "invalid_network_peer", "Не удалось проверить подключение к KPDesk. Попробуйте позже.")
    return request.client.host


@router.post("/register", status_code=202)
def register_account(
    body: RegisterRequest, request: Request, response: Response,
    background: BackgroundTasks,
    auth_engine: Annotated[Engine, Depends(get_unreleased_login_engine)],
    email_dispatch: Annotated[
        AccountEmailDispatcher, Depends(get_account_email_dispatcher),
    ],
) -> dict[str, str]:
    try:
        created = AccountLifecycle(auth_engine).register(
            body.email, body.password, body.organization_name,
            peer=_peer(request),
        )
    except (LoginDenied, ValueError) as exc:
        raise ApiError(422, "invalid_registration", "Не удалось зарегистрировать аккаунт. Проверьте введённые данные или повторите попытку позже.") from exc
    if created:
        background.add_task(email_dispatch, auth_engine)
    response.headers["Cache-Control"] = "no-store"
    return {"status": "accepted"}


class ResendConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=1, max_length=256)


@router.post("/resend-confirmation", status_code=202)
def resend_confirmation(
    body: ResendConfirmationRequest, request: Request, response: Response,
    background: BackgroundTasks,
    auth_engine: Annotated[Engine, Depends(get_unreleased_login_engine)],
    email_dispatch: Annotated[
        AccountEmailDispatcher, Depends(get_account_email_dispatcher),
    ],
) -> dict[str, str]:
    # Generic status never reveals account existence or password correctness.
    created = AccountLifecycle(auth_engine).resend_confirmation(
        body.email, body.password, peer=_peer(request),
    )
    if created:
        background.add_task(email_dispatch, auth_engine)
    response.headers["Cache-Control"] = "no-store"
    return {"status": "accepted"}


@router.post("/verify-email", response_model=VerifiedAccountResponse)
def verify_email(
    body: TokenRequest, response: Response,
    auth_engine: Annotated[Engine, Depends(get_unreleased_login_engine)],
) -> VerifiedAccountResponse:
    result = AccountLifecycle(auth_engine).verify_email(body.token)
    if result is None:
        raise ApiError(400, "invalid_email_token", "Ссылка подтверждения недействительна или срок её действия истёк. Запросите новое письмо.")
    response.headers["Cache-Control"] = "no-store"
    return VerifiedAccountResponse(
        user_id=result.user_id, organization_id=result.organization_id,
    )


@router.post("/invitations/register", response_model=VerifiedAccountResponse)
def join_invited_organization(
    body: JoinInvitationRequest, response: Response,
    auth_engine: Annotated[Engine, Depends(get_unreleased_login_engine)],
) -> VerifiedAccountResponse:
    try:
        account = register_invited(
            auth_engine, token=body.token, password=body.password,
        )
    except ValueError as exc:
        raise ApiError(422, "invalid_password", "Пароль не соответствует требованиям. Проверьте его длину и допустимые символы.") from exc
    if account is None:
        raise ApiError(400, "invalid_invitation_token",
                       "Приглашение недействительно или срок его действия истёк. Попросите руководителя прислать новое.")
    response.headers["Cache-Control"] = "no-store"
    return VerifiedAccountResponse(
        user_id=account.user_id, organization_id=account.organization_id,
    )


@router.post("/password/forgot", status_code=202)
def forgot_password(
    body: ForgotPasswordRequest, request: Request, response: Response,
    background: BackgroundTasks,
    auth_engine: Annotated[Engine, Depends(get_unreleased_login_engine)],
    email_dispatch: Annotated[
        AccountEmailDispatcher, Depends(get_account_email_dispatcher),
    ],
) -> dict[str, str]:
    queued = AccountLifecycle(auth_engine).request_password_reset(
        body.email, peer=_peer(request),
    )
    if queued:
        background.add_task(email_dispatch, auth_engine)
    response.headers["Cache-Control"] = "no-store"
    return {"status": "accepted"}


@router.post("/password/reset", status_code=204)
def reset_password(
    body: ResetPasswordRequest, response: Response,
    auth_engine: Annotated[Engine, Depends(get_unreleased_login_engine)],
) -> Response:
    try:
        changed = AccountLifecycle(auth_engine).reset_password(body.token, body.password)
    except ValueError as exc:
        raise ApiError(422, "invalid_password", "Пароль не соответствует требованиям. Проверьте его длину и допустимые символы.") from exc
    if not changed:
        raise ApiError(400, "invalid_reset_token", "Ссылка для смены пароля недействительна или срок её действия истёк. Запросите новую ссылку.")
    response.status_code = 204
    response.headers["Cache-Control"] = "no-store"
    return response
