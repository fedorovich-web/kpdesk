"""Authenticated, consent-gated LLM deduplication on private PostgreSQL.

No customer source text is stored. Results are still untrusted draft data and
each replay obtains a distinct, quota-metered, server-sealable extraction ID.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import uuid
from dataclasses import dataclass
from typing import Literal

from cryptography.fernet import Fernet, InvalidToken
from dotenv import dotenv_values
from sqlalchemy import Engine, text
from sqlalchemy.dialects.postgresql import BYTEA
from sqlalchemy.sql import bindparam

from kpdesk.config import PROJECT_ROOT
from kpdesk.db.authenticated_tenant import bind_authenticated_tenant
from kpdesk.db.llm_budget import inference_transaction
from kpdesk.extraction.schema import EXTRACTION_PROMPT_VERSION

# The installed 0030 SQL accepts this storage protocol identifier. Inference
# contract is independently included in the HMAC, so older results cannot hit.
_CACHE_STORAGE_PROTOCOL = "kpdesk_request_v3"
_SECRET_HEX = re.compile(r"[0-9a-fA-F]{64}\Z")


@dataclass(frozen=True, slots=True)
class CacheClaim:
    status: Literal["hit", "miss", "busy"]
    payload: bytes | None


def cache_secret() -> bytes | None:
    values = dotenv_values(PROJECT_ROOT / ".env")
    enabled = os.environ.get(
        "KP_DESK_ENABLE_LLM_CACHE", values.get("KP_DESK_ENABLE_LLM_CACHE")
    ) == "1"
    if not enabled:
        return None
    supplied = os.environ.get("KP_DESK_LLM_CACHE_HASH_KEY")
    if supplied is None:
        supplied = values.get("KP_DESK_LLM_CACHE_HASH_KEY")
    if not isinstance(supplied, str) or _SECRET_HEX.fullmatch(supplied) is None:
        return None
    if cache_cipher() is None:
        return None
    return bytes.fromhex(supplied)


def cache_cipher() -> Fernet | None:
    supplied = os.environ.get("KP_DESK_LLM_CACHE_FERNET_KEY")
    if supplied is None:
        supplied = dotenv_values(PROJECT_ROOT / ".env").get("KP_DESK_LLM_CACHE_FERNET_KEY")
    if not isinstance(supplied, str):
        return None
    try:
        return Fernet(supplied.encode("ascii"))
    except (UnicodeEncodeError, ValueError):
        return None


def encrypt_cached_payload(payload: dict[str, object]) -> bytes:
    cipher = cache_cipher()
    if cipher is None:
        raise RuntimeError("Cache encryption key unavailable.")
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return cipher.encrypt(serialized)


def decrypt_cached_payload(ciphertext: bytes) -> dict[str, object]:
    cipher = cache_cipher()
    if cipher is None:
        raise RuntimeError("Cache encryption key unavailable.")
    try:
        raw = json.loads(cipher.decrypt(ciphertext))
    except (InvalidToken, ValueError, UnicodeDecodeError) as error:
        raise RuntimeError("Encrypted cache payload invalid.") from error
    if not isinstance(raw, dict):
        raise TypeError("Encrypted cache payload has invalid shape.")
    return raw


def extraction_hmac(secret: bytes, *, text_source: str, model: str) -> str:
    """Exact-text match. Include schema/model to prevent stale cross-model reuse."""
    if len(secret) != 32:
        raise ValueError("A distinct 32-byte cache secret is required.")
    canonical = json.dumps(
        {"schema": EXTRACTION_PROMPT_VERSION, "provider": "openrouter", "model": model, "text": text_source},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(secret, canonical, hashlib.sha256).hexdigest()


def claim_cached_extraction(
    engine: Engine, *, organization_id: uuid.UUID, attempt_id: uuid.UUID,
    session_cookie: str, csrf: str, request_hmac: str, model: str,
) -> CacheClaim:
    with inference_transaction(engine) as connection:
        bind_authenticated_tenant(
            connection, session_token=session_cookie, organization_id=organization_id,
            operation="write", csrf=csrf,
        )
        row = connection.execute(text("""
            SELECT cache_status,cache_payload
            FROM public.kpdesk_llm_cache_claim(
                :org,:attempt,:hash,:model,:version
            )
        """), {
            "org": organization_id, "attempt": attempt_id,
            "hash": request_hmac, "provider": "openrouter", "model": model, "version": _CACHE_STORAGE_PROTOCOL,
        }).one()
        status, payload = row
    if status == "forbidden":
        raise RuntimeError("Cache claim rejected by tenant SQL policy.")
    if status not in ("hit", "miss", "busy"):
        raise RuntimeError("Unexpected cache admission result.")
    if status == "hit" and not isinstance(payload, bytes):
        raise RuntimeError("Cached extraction payload invalid.")
    return CacheClaim(status, payload if isinstance(payload, bytes) else None)


def publish_cached_extraction(
    engine: Engine, *, organization_id: uuid.UUID, attempt_id: uuid.UUID,
    session_cookie: str, csrf: str, request_hmac: str,
    extracted: dict[str, object],
) -> bool:
    ciphertext = encrypt_cached_payload(extracted)
    if len(ciphertext) > 65536:
        return False
    with inference_transaction(engine) as connection:
        bind_authenticated_tenant(
            connection, session_token=session_cookie, organization_id=organization_id,
            operation="write", csrf=csrf,
        )
        accepted = connection.scalar(text("""
            SELECT public.kpdesk_llm_cache_publish(:org,:attempt,:hash,:payload)
        """).bindparams(bindparam("payload", type_=BYTEA)), {
            "org": organization_id, "attempt": attempt_id, "hash": request_hmac,
            "payload": ciphertext,
        })
        return accepted is True


def release_cached_extraction(
    engine: Engine, *, organization_id: uuid.UUID, attempt_id: uuid.UUID,
    session_cookie: str, csrf: str, request_hmac: str,
) -> None:
    with inference_transaction(engine) as connection:
        bind_authenticated_tenant(
            connection, session_token=session_cookie, organization_id=organization_id,
            operation="write", csrf=csrf,
        )
        connection.scalar(text("""
            SELECT public.kpdesk_llm_cache_release(:org,:attempt,:hash)
        """), {
            "org": organization_id, "attempt": attempt_id, "hash": request_hmac,
        })
