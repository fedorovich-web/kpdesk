"""Verified account lifecycle backed by restricted SECURITY DEFINER SQL functions.

Only SHA256 challenge digests enter PostgreSQL outside Fernet-encrypted email
outbox envelopes. No raw token, password or email secret is logged or returned.
"""
from __future__ import annotations

import hashlib
import ipaddress
import re
import secrets
import uuid
from dataclasses import dataclass

from cryptography.fernet import Fernet
from sqlalchemy import Engine, text

from kpdesk.config import PROJECT_ROOT
from kpdesk.db.auth_sessions import (
    LoginDenied,
    _normalized_email,
    hash_password,
    verify_password,
)

_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_KEY_FILE = PROJECT_ROOT / "credentials" / "kpdesk-email-envelope.key"


def _cipher() -> Fernet:
    if not _KEY_FILE.is_file():
        raise RuntimeError("Account email encryption is not provisioned.")
    return Fernet(_KEY_FILE.read_bytes().strip())


def _digest(raw_token: str) -> str | None:
    if not isinstance(raw_token, str) or _TOKEN.fullmatch(raw_token) is None:
        return None
    return hashlib.sha256(raw_token.encode("ascii")).hexdigest()


def _prepared_challenge() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    digest = _digest(token)
    if digest is None:
        raise RuntimeError("Secure token generation failed.")
    envelope = _cipher().encrypt(token.encode("ascii")).decode("ascii")
    return digest, envelope


def _peer(address: str) -> str:
    try:
        return str(ipaddress.ip_address(address))
    except (ValueError, TypeError) as exc:
        raise ValueError("Unrecognized network peer.") from exc


@dataclass(frozen=True, slots=True)
class VerifiedMembership:
    user_id: uuid.UUID
    organization_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class AccountLifecycle:
    auth_engine: Engine

    def register(
        self, email: str, password: str, organization_name: str,
        *, peer: str,
    ) -> bool:
        normalized = _normalized_email(email)
        if (
            not isinstance(organization_name, str)
            or not 1 <= len(organization_name.strip()) <= 200
        ):
            raise ValueError("Invalid organization name.")
        password_hash = hash_password(password)
        digest, envelope = _prepared_challenge()
        with self.auth_engine.begin() as conn:
            return conn.scalar(text("""
                SELECT public.kpdesk_account_signup(
                    :email,:password_hash,:organization,:digest,:cipher,:peer
                )
            """), {
                "email": normalized, "password_hash": password_hash,
                "organization": organization_name.strip(),
                "digest": digest, "cipher": envelope, "peer": _peer(peer),
            }) is True

    def resend_confirmation(self, email: str, password: str, *, peer: str) -> bool:
        try:
            normalized = _normalized_email(email)
        except LoginDenied:
            verify_password(None, password)
            return False
        with self.auth_engine.begin() as conn:
            stored_hash = conn.scalar(text("""
                SELECT public.kpdesk_account_resend_credential(:email,:peer)
            """), {"email": normalized, "peer": _peer(peer)})
        if not verify_password(stored_hash, password):
            return False
        digest, envelope = _prepared_challenge()
        with self.auth_engine.begin() as conn:
            return conn.scalar(text("""
                SELECT public.kpdesk_account_resend_issue(
                    :email,:hash,:digest,:cipher
                )
            """), {
                "email": normalized, "hash": stored_hash,
                "digest": digest, "cipher": envelope,
            }) is True

    def verify_email(self, raw_token: str) -> VerifiedMembership | None:
        digest = _digest(raw_token)
        if digest is None:
            return None
        with self.auth_engine.begin() as conn:
            row = conn.execute(text("""
                SELECT identity_id,organization_id
                FROM public.kpdesk_account_verify(:digest)
            """), {"digest": digest}).one_or_none()
        if row is None:
            return None
        return VerifiedMembership(row.identity_id, row.organization_id)

    def request_password_reset(self, email: str, *, peer: str) -> bool:
        try:
            normalized = _normalized_email(email)
        except LoginDenied:
            # Never disclose whether an account exists.
            return False
        digest, envelope = _prepared_challenge()
        with self.auth_engine.begin() as conn:
            return conn.scalar(text("""
                SELECT public.kpdesk_account_reset_request(
                    :email,:digest,:cipher,:peer
                )
            """), {
                "email": normalized, "digest": digest,
                "cipher": envelope, "peer": _peer(peer),
            }) is True

    def reset_password(self, raw_token: str, password: str) -> bool:
        digest = _digest(raw_token)
        if digest is None:
            return False
        secret_hash = hash_password(password)
        with self.auth_engine.begin() as conn:
            return conn.scalar(text("""
                SELECT public.kpdesk_account_reset_consume(:digest,:password_hash)
            """), {"digest": digest, "password_hash": secret_hash}) is True
