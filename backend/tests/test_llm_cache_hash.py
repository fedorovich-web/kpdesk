"""A51 cache eligibility and HMAC scoping without network or user content logs."""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from cryptography.fernet import Fernet

from kpdesk.db.llm_cache import (
    cache_secret,
    decrypt_cached_payload,
    encrypt_cached_payload,
    extraction_hmac,
)


def test_opt_in_requires_explicit_flag_and_independent_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "ab" * 32
    monkeypatch.setenv("KP_DESK_LLM_CACHE_HASH_KEY", key)
    monkeypatch.setenv("KP_DESK_LLM_CACHE_FERNET_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("KP_DESK_ENABLE_LLM_CACHE", "0")
    assert cache_secret() is None
    monkeypatch.setenv("KP_DESK_ENABLE_LLM_CACHE", "1")
    assert cache_secret() == bytes.fromhex(key)
    monkeypatch.setenv("KP_DESK_LLM_CACHE_HASH_KEY", "weak-secret")
    assert cache_secret() is None


def test_encrypted_payload_is_authenticated_and_not_plaintext(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KP_DESK_LLM_CACHE_FERNET_KEY", Fernet.generate_key().decode("ascii"))
    payload: dict[str, object] = {"lines": [{"source_quote": "Secret synthetic request"}]}
    encrypted = encrypt_cached_payload(payload)
    assert b"Secret synthetic request" not in encrypted
    assert decrypt_cached_payload(encrypted) == payload
    with pytest.raises(RuntimeError):
        decrypt_cached_payload(encrypted[:-1] + b"X")


def test_hmac_is_stable_exact_text_and_model_scoped() -> None:
    key = b"1" * 32
    source = "2 пачки бумаги А4 80 г/м²"
    baseline = extraction_hmac(key, text_source=source, model="openai/gpt-oss-20b")
    assert len(baseline) == 64
    assert baseline == extraction_hmac(key, text_source=source, model="openai/gpt-oss-20b")
    assert baseline != extraction_hmac(
        key, text_source=source + "\n", model="openai/gpt-oss-20b",
    )
    assert baseline != extraction_hmac(
        key, text_source=source, model="openai/gpt-oss-120b",
    )
    assert source not in baseline
    with pytest.raises(ValueError):
        extraction_hmac(b"weak", text_source=source, model="openai/gpt-oss-20b")


def test_current_source_quote_contract_cannot_reuse_v12_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kpdesk.db import llm_cache

    key = b'1' * 32
    source = 'Нужно 2 штуки: Линейка металлическая 50 см.'
    model = 'nvidia/nemotron-3-ultra-550b-a55b:free'
    current = extraction_hmac(key, text_source=source, model=model)
    monkeypatch.setattr(llm_cache, 'EXTRACTION_PROMPT_VERSION', 'kpdesk_request_v12')
    previous = extraction_hmac(key, text_source=source, model=model)
    assert current != previous


@pytest.mark.parametrize('legacy_contract', ['kpdesk_request_v4', 'kpdesk_request_v5', 'kpdesk_request_v6', 'kpdesk_request_v7', 'kpdesk_request_v8', 'kpdesk_request_v9', 'kpdesk_request_v10'])
def test_sparse_extraction_contract_cannot_replay_an_older_cache_entry(legacy_contract: str) -> None:
    key = b'1' * 32
    source = '2 пачки бумаги А4 80 г/м²'
    legacy = json.dumps({'schema': legacy_contract, 'model': 'openai/gpt-oss-20b', 'text': source},
                        sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    assert extraction_hmac(key, text_source=source, model='openai/gpt-oss-20b') != hmac.new(
        key, legacy, hashlib.sha256,
    ).hexdigest()
