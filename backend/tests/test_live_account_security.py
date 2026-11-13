"""A15 account security boundary tests; never register users in working DB."""
from __future__ import annotations

import hashlib

import pytest
from cryptography.fernet import InvalidToken
from fastapi.testclient import TestClient

from kpdesk.db.account_lifecycle import (
    _cipher,
    _digest,
    _peer,
    _prepared_challenge,
)
from kpdesk.main import create_app


def test_one_time_envelope_stores_encrypted_payload_and_digest_only() -> None:
    hash_a, cipher_a = _prepared_challenge()
    hash_b, cipher_b = _prepared_challenge()
    assert hash_a != hash_b
    assert cipher_a != cipher_b
    token = _cipher().decrypt(cipher_a.encode("ascii")).decode("ascii")
    assert _digest(token) == hash_a
    assert token not in cipher_a
    assert hashlib.sha256(token.encode("ascii")).hexdigest() == hash_a
    with pytest.raises(InvalidToken):
        _cipher().decrypt(b"not-a-valid-token")


@pytest.mark.parametrize("raw", [
    "", "x" * 43, "!!" * 25, "a" * 42, "a" * 44, "случайный токен",
])
def test_malformed_email_verification_token_does_not_hash(raw: str) -> None:
    if raw == "x" * 43 or raw == "a" * 43:
        # Safe syntactic token is intentionally not proof of DB existence.
        assert _digest(raw) is not None
    else:
        assert _digest(raw) is None


def test_peer_normalization_fails_closed() -> None:
    assert _peer("127.0.0.1") == "127.0.0.1"
    assert _peer("::1") == "::1"
    with pytest.raises(ValueError):
        _peer("spoofed-peer")


def test_account_routes_exist_only_on_explicit_https_tenant_app() -> None:
    normal = create_app(testing=True)
    with TestClient(normal) as client:
        assert client.post(
            "/api/session/register",
            headers={"Origin": "http://localhost:5173"},
            json={"email": "x", "password": "y", "organization_name": "z"},
        ).status_code == 404
    app = create_app(testing=True, tenant_sessions=True)
    with TestClient(app, base_url="https://testserver") as client:
        origin = {"Origin": "https://localhost:5173"}
        assert client.post(
            "/api/session/register", headers=origin,
            json={"email": "invalid", "password": "short", "organization_name": ""},
        ).status_code == 422
        assert client.post(
            "/api/session/verify-email", headers=origin,
            json={"token": "a" * 43},
        ).status_code == 400
        assert client.post(
            "/api/session/password/reset", headers=origin,
            json={"token": "a" * 43, "password": "StrongEnoughPassword!2026"},
        ).status_code == 400
        assert client.post(
            "/api/session/password/forgot", headers=origin,
            json={"email": "not-an-email"},
        ).status_code == 202
