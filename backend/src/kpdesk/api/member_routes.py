"""Member invitations and role changes through session-bound PostgreSQL gateways.

Only verified existing accounts can currently be invited. The invitee must
explicitly accept while authenticated; an email alone is not authorization.
"""
from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, Header, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, text

from kpdesk.api.errors import ApiError
from kpdesk.api.tenant_session_routes import (
    SESSION_COOKIE,
    _required_session,
    get_unreleased_auth_repository,
    get_unreleased_login_engine,
)
from kpdesk.db.auth_sessions import AuthSessionRepository, _token_digest
from kpdesk.db.team_email_invites import invite_unregistered
from kpdesk.notifications.team_invite_outbox import dispatch_team_one

router = APIRouter(prefix="/api/session")


type TeamEmailDispatcher = Callable[[Engine], Awaitable[str]]


def get_team_email_dispatcher() -> TeamEmailDispatcher:
    return dispatch_team_one


class InviteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    email: str = Field(min_length=5, max_length=254)
    role: Literal["editor", "viewer"]


class InvitationResult(BaseModel):
    invitation_id: uuid.UUID


class PendingInvitation(BaseModel):
    invitation_id: uuid.UUID
    organization_id: uuid.UUID
    organization_name: str
    proposed_role: Literal["editor", "viewer"]
    expires_at: str


class MemberSummary(BaseModel):
    user_id: uuid.UUID
    email: str
    role: Literal["owner", "editor", "viewer"]
    status: Literal["active", "revoked"]


class ChangeRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    role: Literal["editor", "viewer"]


def _actor(
    repo: AuthSessionRepository, cookie: str | None, csrf: str | None = None,
    *, write: bool = False,
) -> str:
    _required_session(repo, cookie, csrf=csrf, write=write)
    return _token_digest(cookie or "")


@router.post("/organizations/{organization_id}/invitations",
             response_model=InvitationResult, status_code=201)
def invite_member(
    organization_id: uuid.UUID, body: InviteRequest, response: Response,
    repo: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> InvitationResult:
    digest = _actor(repo, cookie, csrf, write=True)
    invite_id = repo.connection.scalar(
        text("""
            SELECT public.kpdesk_member_invite(:digest,:org,:email,:role)
        """), {"digest": digest, "org": organization_id,
               "email": body.email.lower().strip(), "role": body.role},
    )
    if invite_id is None:
        raise ApiError(403, "invitation_denied",
                       "Пригласить сотрудника может руководитель организации. У сотрудника должен быть подтверждённый аккаунт.")
    response.headers["Cache-Control"] = "no-store"
    return InvitationResult(invitation_id=invite_id)


@router.post("/organizations/{organization_id}/invitations/email", status_code=202)
def invite_new_email(
    organization_id: uuid.UUID, body: InviteRequest, response: Response,
    background: BackgroundTasks,
    repo: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    auth_engine: Annotated[Engine, Depends(get_unreleased_login_engine)],
    dispatcher: Annotated[TeamEmailDispatcher, Depends(get_team_email_dispatcher)],
    cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> dict[str, str]:
    digest = _actor(repo, cookie, csrf, write=True)
    authorized = repo.connection.scalar(text("""
        SELECT public.kpdesk_member_actor(:session,:org,true)
    """), {"session": digest, "org": organization_id})
    if authorized is None:
        raise ApiError(403, "organization_forbidden", "Приглашать сотрудников может только руководитель организации.")
    created = invite_unregistered(
        repo.connection, session=cookie or "",
        organization=organization_id,
        email=body.email, role=body.role,
    )
    if created:
        background.add_task(dispatcher, auth_engine)
    response.headers["Cache-Control"] = "no-store"
    # Generic response avoids enumerating registered vs unregistered addresses.
    return {"status": "accepted"}


@router.get("/invitations", response_model=list[PendingInvitation])
def pending_invitations(
    response: Response,
    repo: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> list[PendingInvitation]:
    digest = _actor(repo, cookie)
    rows = repo.connection.execute(text("""
        SELECT invitation_id,organization_id,organization_name,proposed_role,expires_at
        FROM public.kpdesk_member_pending(:digest)
    """), {"digest": digest}).all()
    response.headers["Cache-Control"] = "no-store"
    return [
        PendingInvitation(
            invitation_id=row.invitation_id, organization_id=row.organization_id,
            organization_name=row.organization_name,
            proposed_role=row.proposed_role, expires_at=row.expires_at.isoformat(),
        )
        for row in rows
    ]


@router.post("/invitations/{invitation_id}/accept", status_code=204)
def accept_invitation(
    invitation_id: uuid.UUID, response: Response,
    repo: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> Response:
    digest = _actor(repo, cookie, csrf, write=True)
    organization = repo.connection.scalar(text("""
        SELECT public.kpdesk_member_accept(:digest,:invite)
    """), {"digest": digest, "invite": invitation_id})
    if organization is None:
        raise ApiError(403, "invitation_denied",
                       "Не удалось принять приглашение. Проверьте аккаунт и срок действия приглашения.")
    response.headers["Cache-Control"] = "no-store"
    response.status_code = 204
    return response


@router.get("/organizations/{organization_id}/members",
            response_model=list[MemberSummary])
def list_members(
    organization_id: uuid.UUID, response: Response,
    repo: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> list[MemberSummary]:
    digest = _actor(repo, cookie)
    rows = repo.connection.execute(text("""
        SELECT user_id,member_email,membership_role,membership_status
        FROM public.kpdesk_member_list(:digest,:org)
    """), {"digest": digest, "org": organization_id}).all()
    # A non-owner must never infer if there are zero members or if the
    # organization exists. Distinguish denial using a narrow owner test.
    is_owner = repo.connection.scalar(text("""
        SELECT public.kpdesk_member_actor(:digest,:org,true)
    """), {"digest": digest, "org": organization_id})
    if is_owner is None:
        raise ApiError(403, "organization_forbidden", "Для управления командой нужны права руководителя организации.")
    response.headers["Cache-Control"] = "no-store"
    return [
        MemberSummary(
            user_id=row.user_id, email=row.member_email,
            role=row.membership_role, status=row.membership_status,
        )
        for row in rows
    ]


@router.patch("/organizations/{organization_id}/members/{user_id}",
              status_code=204)
def change_member_role(
    organization_id: uuid.UUID, user_id: uuid.UUID,
    body: ChangeRoleRequest, response: Response,
    repo: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> Response:
    return _change(repo, cookie, csrf, response, organization_id, user_id,
                   body.role, revoke=False)


@router.delete("/organizations/{organization_id}/members/{user_id}",
               status_code=204)
def revoke_member(
    organization_id: uuid.UUID, user_id: uuid.UUID, response: Response,
    repo: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> Response:
    return _change(repo, cookie, csrf, response, organization_id, user_id,
                   "viewer", revoke=True)


def _change(
    repo: AuthSessionRepository, cookie: str | None, csrf: str | None,
    response: Response, organization_id: uuid.UUID, user_id: uuid.UUID,
    role: str, *, revoke: bool,
) -> Response:
    digest = _actor(repo, cookie, csrf, write=True)
    success = repo.connection.scalar(text("""
        SELECT public.kpdesk_member_change(
            :digest,:org,:target,:role,:revoke
        )
    """), {"digest": digest, "org": organization_id, "target": user_id,
           "role": role, "revoke": revoke})
    if success is not True:
        raise ApiError(403, "member_change_denied",
                       "Изменять доступ сотрудников может только руководитель. Доступ руководителя этим действием не изменяется.")
    response.headers["Cache-Control"] = "no-store"
    response.status_code = 204
    return response
