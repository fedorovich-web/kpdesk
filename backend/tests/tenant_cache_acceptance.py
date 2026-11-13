"""A51 isolated-clone acceptance: real RLS functions and no external Groq."""
from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from unittest.mock import patch

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from kpdesk.api.routes import get_groq_client, get_groq_settings
from kpdesk.api.tenant_session_routes import SESSION_COOKIE
from kpdesk.db.auth_sessions import IssuedSession
from kpdesk.db.llm_budget import finish_inference, reserve_inference
from kpdesk.db.llm_cache import claim_cached_extraction, extraction_hmac, release_cached_extraction
from kpdesk.extraction.groq import GroqSettings

_SOURCE = "2 пачки бумаги А4 80 г/м²"
_MODEL = "openai/gpt-oss-20b"


def verify_tenant_cache_http(
    app: FastAPI, owner: Engine, tenant: Engine,
    organization: uuid.UUID, outsider: uuid.UUID,
    first: IssuedSession, second: IssuedSession,
) -> None:
    calls: list[int] = []
    payload = {
        "lines": [{
            "source_quote": _SOURCE, "description": "Бумага А4 80 г/м²",
            "category": "paper", "quantity": 2, "unit": "пачка",
            "brand": None, "paper_format": "A4", "paper_gsm": 80,
            "pen_ink_color": None, "pen_ink_type": None, "attributes": [],
        }],
        "instruction_quotes": [],
    }

    def synthetic_provider(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.groq.com"
        calls.append(1)
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps(payload, ensure_ascii=False),
            }}],
        })

    async def stub_provider() -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(synthetic_provider),
        ) as client:
            yield client

    app.dependency_overrides[get_groq_client] = stub_provider
    # Historical Groq harness remains incompatible with the current provider.
    # Validate runtime input explicitly; do not bypass the locked model contract.
    app.dependency_overrides[get_groq_settings] = lambda: GroqSettings.model_validate({
        "api_key": "never-sent-over-network", "model": _MODEL,
    })
    secret = bytes.fromhex("11" * 32)
    with patch.dict(os.environ, {
        "KP_DESK_ENABLE_LLM_CACHE": "1",
        "KP_DESK_LLM_CACHE_HASH_KEY": secret.hex(),
        "KP_DESK_LLM_CACHE_FERNET_KEY": Fernet.generate_key().decode("ascii"),
    }):
        with TestClient(app, base_url="https://testserver") as client:
            client.headers["Origin"] = "https://testserver"
            url = f"/api/session/organizations/{organization}/requests/extract"
            body = {"text": _SOURCE, "consent_to_external_processing": True}
            assert client.post(url, json=body).status_code == 401
            client.cookies.set(SESSION_COOKIE, first.session_token)
            first_response = client.post(
                url, headers={"X-CSRF-Token": first.csrf_token}, json=body,
            )
            assert first_response.status_code == 200, first_response.text
            assert first_response.json()["cache_hit"] is False
            assert calls == [1]
            second_response = client.post(
                url, headers={"X-CSRF-Token": first.csrf_token}, json=body,
            )
            assert second_response.status_code == 200, second_response.text
            assert second_response.json()["cache_hit"] is True
            assert calls == [1]
            assert first_response.json()["extraction_id"] != (
                second_response.json()["extraction_id"]
            )
            client.cookies.set(SESSION_COOKIE, second.session_token)
            third_response = client.post(
                url, headers={"X-CSRF-Token": second.csrf_token}, json=body,
            )
            assert third_response.status_code == 200, third_response.text
            assert third_response.json()["cache_hit"] is True
            assert calls == [1]
            assert len({
                first_response.json()["extraction_id"],
                second_response.json()["extraction_id"],
                third_response.json()["extraction_id"],
            }) == 3
            assert client.post(
                url.replace(str(organization), str(outsider)),
                headers={"X-CSRF-Token": second.csrf_token}, json=body,
            ).status_code == 403
            assert calls == [1]

        identifiers = tuple(
            uuid.UUID(row["extraction_id"])
            for row in (
                first_response.json(), second_response.json(),
                third_response.json(),
            )
        )
        with owner.connect() as connection:
            cached = connection.execute(text("""
                SELECT status,extracted_payload,model
                FROM public.tenant_llm_extraction_cache
                WHERE organization_id=:org
            """), {"org": organization}).one()
            assert cached[0] == "ready"
            assert cached[2] == _MODEL
            assert isinstance(cached[1], bytes)
            assert _SOURCE.encode("utf-8") not in cached[1]
            rows = connection.execute(text("""
                SELECT id,user_id,status,source_sha256,preview_sha256
                FROM public.tenant_llm_attempts
                WHERE id=ANY(:ids)
            """), {"ids": list(identifiers)}).all()
            assert len(rows) == 3
            assert all(
                row.status == "success" and
                row.source_sha256 is not None and
                row.preview_sha256 is not None
                for row in rows
            )
            assert sum(row.user_id == first.user_id for row in rows) == 2
            assert sum(row.user_id == second.user_id for row in rows) == 1
            assert connection.scalar(text("""
                SELECT has_table_privilege(
                    'kpdesk_tenant_runtime',
                    'public.tenant_llm_extraction_cache','SELECT'
                )
            """)) is False

        # Cross-user identical *pending* requests must not double-bill Groq.
        other_text = "6 пачек белой бумаги А4"
        digest = extraction_hmac(
            secret, text_source=other_text, model=_MODEL,
        )
        pending_a = reserve_inference(
            tenant, session_cookie=first.session_token, csrf=first.csrf_token,
            organization_id=organization, model=_MODEL, input_chars=len(other_text),
        )
        try:
            assert claim_cached_extraction(
                tenant, organization_id=organization, attempt_id=pending_a,
                session_cookie=first.session_token, csrf=first.csrf_token,
                request_hmac=digest, model=_MODEL,
            ).status == "miss"
            pending_b = reserve_inference(
                tenant, session_cookie=second.session_token, csrf=second.csrf_token,
                organization_id=organization, model=_MODEL, input_chars=len(other_text),
            )
            try:
                assert claim_cached_extraction(
                    tenant, organization_id=organization, attempt_id=pending_b,
                    session_cookie=second.session_token, csrf=second.csrf_token,
                    request_hmac=digest, model=_MODEL,
                ).status == "busy"
            finally:
                finish_inference(
                    tenant, request_id=pending_b,
                    session_cookie=second.session_token, succeeded=False,
                )
        finally:
            finish_inference(
                tenant, request_id=pending_a,
                session_cookie=first.session_token, succeeded=False,
            )
            release_cached_extraction(
                tenant, organization_id=organization, attempt_id=pending_a,
                session_cookie=first.session_token, csrf=first.csrf_token,
                request_hmac=digest,
            )
        assert calls == [1]
