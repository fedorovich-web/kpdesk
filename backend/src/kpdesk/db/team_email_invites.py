"""Unregistered invite delivery tokens and verified team join."""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass

from sqlalchemy import Connection, Engine, text

from kpdesk.db.account_lifecycle import _cipher
from kpdesk.db.auth_sessions import LoginDenied, _normalized_email, hash_password

_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")


def _token_digest(value: str) -> str | None:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        return None
    return hashlib.sha256(value.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class InvitedAccount:
    user_id: uuid.UUID
    organization_id: uuid.UUID


def invite_unregistered(
    connection: Connection, *, session: str, organization: uuid.UUID,
    email: str, role: str,
) -> bool:
    try:
        recipient = _normalized_email(email)
    except LoginDenied:
        return False
    if role not in ("viewer", "editor"):
        return False
    raw = secrets.token_urlsafe(32)
    digest = _token_digest(raw)
    if digest is None:
        raise RuntimeError("Secure invitation token generation failed.")
    cipher = _cipher().encrypt(raw.encode("ascii")).decode("ascii")
    result = connection.scalar(text("""
        SELECT public.kpdesk_email_invite_issue(
            :session,:org,:email,:role,:digest,:cipher
        )
    """), {
        "session": hashlib.sha256(session.encode("ascii")).hexdigest(),
        "org": organization, "email": recipient, "role": role,
        "digest": digest, "cipher": cipher,
    })
    return isinstance(result, uuid.UUID)


def register_invited(
    auth_engine: Engine, *, token: str, password: str,
) -> InvitedAccount | None:
    digest = _token_digest(token)
    if digest is None:
        return None
    password_hash = hash_password(password)
    with auth_engine.begin() as connection:
        row = connection.execute(text("""
            SELECT user_id,organization_id
            FROM public.kpdesk_email_invite_accept(:digest,:hash)
        """), {"digest": digest, "hash": password_hash}).one_or_none()
    if row is None:
        return None
    return InvitedAccount(user_id=row.user_id, organization_id=row.organization_id)
