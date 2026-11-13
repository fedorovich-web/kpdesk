"""Real HTTP A15 registration→email→tenant catalog/quote→password reset E2E.

Disposable marked copy of the actual 0015 working DB, genuine independent
PostgreSQL LOGIN identities and MockTransport Resend. Zero live customer writes.
"""
from __future__ import annotations

import asyncio
import csv
import importlib
import io
import json
import re
import secrets
import subprocess
import uuid
from collections.abc import AsyncIterator

import httpx
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from dotenv import dotenv_values
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from kpdesk.api.account_routes import get_account_email_dispatcher
from kpdesk.api.member_routes import get_team_email_dispatcher
from kpdesk.api.routes import get_groq_client, get_groq_settings
from kpdesk.api.tenant_session_routes import (
    SESSION_COOKIE,
    get_unreleased_catalog_engine,
    get_unreleased_login_engine,
)
from kpdesk.catalog.schema import COLUMNS_V2
from kpdesk.config import PROJECT_ROOT
from kpdesk.db.llm_budget import (
    InferenceQuotaExceeded,
    finish_inference,
    reserve_inference,
)
from kpdesk.extraction.groq import GroqSettings
from kpdesk.main import create_app
from kpdesk.notifications.account_email_outbox import dispatch_one
from kpdesk.notifications.resend_mail import EmailSettings
from kpdesk.notifications.team_invite_outbox import dispatch_team_one
from scripts.rehearse_release_snapshot import _docker_input

_ROOT = PROJECT_ROOT
_EMAIL = re.compile(
    r"https://kpdesk.ru/auth/(verify-email|reset-password|join)\?token=([A-Za-z0-9_-]{43})"
)


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _connection(database: str, login: str, password: str, port: int,
                *, admin: bool = False, runtime: str | None = None) -> Engine:
    engine = create_engine(
        URL.create(
            "postgresql+psycopg", username=login, password=password,
            host="127.0.0.1", port=port, database=database,
        ),
        isolation_level="AUTOCOMMIT" if admin else None,
        poolclass=NullPool, pool_pre_ping=True,
        connect_args={"connect_timeout": 4},
    )
    if runtime is not None:
        @event.listens_for(engine, "begin")
        def _role(connection: Connection) -> None:
            connection.exec_driver_sql("SET LOCAL ROLE " + runtime)
    return engine


def _csv(price: str) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";", lineterminator="\n")
    writer.writerow(COLUMNS_V2)
    item = {
        "sku": "A15-PAPER-A4", "name": "Office paper A4 80",
        "category": "paper", "sale_unit": "пачка",
        "pieces_per_sale_unit": 1, "price_rub": price,
        "paper_format": "A4", "paper_gsm": 80, "attributes_json": "{}",
    }
    writer.writerow([item.get(column, "") for column in COLUMNS_V2])
    return output.getvalue().encode("utf-8")


def verify_http(app: FastAPI, owner: Engine, auth: Engine, tenant: Engine) -> None:
    app.dependency_overrides[get_unreleased_login_engine] = lambda: auth
    app.dependency_overrides[get_unreleased_catalog_engine] = lambda: tenant
    origin = {"Origin": "https://localhost:5173"}
    groq_calls: list[dict[str, object]] = []
    valid_llm_text = (
        "Нужно 2 пачки бумаги А4 80 г/м² и ещё 1 пачку бумаги А4 80 г/м²."
    )
    llm_lines: list[dict[str, object]] = [
        {
            "source_quote": evidence,
            "description": "Бумага А4 80 г/м²",
            "category": "paper", "quantity": amount, "unit": "пачка",
            "brand": None, "paper_format": "A4", "paper_gsm": 80,
            "pen_ink_color": None, "pen_ink_type": None,
            "attributes": {},
        }
        for evidence, amount in (
            ("2 пачки бумаги А4 80 г/м²", 2),
            ("1 пачку бумаги А4 80 г/м²", 1),
        )
    ]
    fabricated_quote = False

    def mock_llm(request: httpx.Request) -> httpx.Response:
        if request.url.host != "openrouter.ai":
            raise RuntimeError("Provider request went to the wrong host.")
        body = json.loads(request.content)
        groq_calls.append(body)
        proposed = [dict(row) for row in llm_lines]
        if fabricated_quote:
            proposed[0]["source_quote"] = "ABSENT FROM ORIGINAL REQUEST"
        return httpx.Response(200, json={
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": json.dumps(
                    {"lines": proposed}, ensure_ascii=False,
                )},
            }],
        })

    async def mock_groq_client() -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(mock_llm),
        ) as test_client:
            yield test_client

    app.dependency_overrides[get_groq_settings] = lambda: GroqSettings(
        api_key="replaced-local-mock-key", model="nvidia/nemotron-3-ultra-550b-a55b:free", test_project_approved=True,
    )
    app.dependency_overrides[get_groq_client] = mock_groq_client
    delivered: dict[tuple[str, str], str] = {}
    transient_mail_failure = {"bob@a15.example"}

    def accept_mail(request: httpx.Request) -> httpx.Response:
        _check(request.url.host == "api.resend.com", "Mock transport leaked external target.")
        payload = json.loads(request.content)
        match = _EMAIL.search(payload["text"])
        if match is None:
            raise RuntimeError("Verification/reset link absent from email.")
        recipient = payload["to"][0]
        if recipient in transient_mail_failure:
            transient_mail_failure.remove(recipient)
            return httpx.Response(503, json={"message": "Temporary provider outage"})
        delivered[(recipient, match.group(1))] = match.group(2)
        return httpx.Response(200, json={"id": "local-mock-confirmation"})

    async def locally_deliver(engine: Engine) -> str:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(accept_mail),
        ) as client:
            state = await dispatch_one(
                engine, client=client, settings=EmailSettings("re_mock"),
            )
            _check(state in ("sent", "failed"), f"Durable outbox delivery: {state}")
            return state

    async def locally_team_deliver(engine: Engine) -> str:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(accept_mail),
        ) as client:
            state = await dispatch_team_one(
                engine, client=client, settings=EmailSettings("re_mock"),
            )
            _check(state in ("sent", "failed"), "Team invite outbox status invalid.")
            return state

    app.dependency_overrides[get_account_email_dispatcher] = lambda: locally_deliver
    app.dependency_overrides[get_team_email_dispatcher] = lambda: locally_team_deliver
    try:
        with TestClient(
            app, base_url="https://testserver", client=("127.0.0.1", 50222),
        ) as alice, TestClient(
            app, base_url="https://testserver", client=("127.0.0.2", 50223),
        ) as bob:
            emails = ("alice@a15.example", "bob@a15.example")
            old_password = "A15InitialPassword!2026"
            for client, email, org_label in (
                (alice, emails[0], "Acme Test Supplies"),
                (bob, emails[1], "Beta Test Supplies"),
            ):
                response = client.post(
                    "/api/session/register", headers=origin,
                    json={
                        "email": email, "password": old_password,
                        "organization_name": org_label,
                    },
                )
                _check(response.status_code == 202, "HTTP registration failed.")
                if email in transient_mail_failure:
                    raise RuntimeError("Unexpected still-pending provider failure.")
                if email == "bob@a15.example":
                    with owner.begin() as conn:
                        _check(conn.scalar(text("""
                            SELECT count(*) FROM account_email_outbox
                            WHERE recipient_email=:email AND status='pending'
                                  AND attempts=1
                        """), {"email": email}) == 1,
                               "Transient mail failure was not persisted for retry.")
                        conn.execute(text("""
                            UPDATE account_email_outbox
                            SET next_attempt_at=clock_timestamp()-interval '1 second'
                            WHERE recipient_email=:email
                        """), {"email": email})
                    retry_result = asyncio.run(locally_deliver(auth))
                    _check(retry_result == "sent", "Outbox retry failed.")
                _check((email, "verify-email") in delivered,
                       "Email was not queued, encrypted and dispatched.")
                _check(client.post(
                    "/api/session/login", headers=origin,
                    json={"email": email, "password": old_password},
                ).status_code == 401, "Unverified user could log in.")
            # Wrong password and too-frequent resend are generic no-ops.
            original_alice = delivered[(emails[0], "verify-email")]
            for pw in ("incorrect-not-her-password", old_password):
                res = alice.post(
                    "/api/session/resend-confirmation", headers=origin,
                    json={"email": emails[0], "password": pw},
                )
                _check(res.status_code == 202, "Resend revealed account state.")
                _check(delivered[(emails[0], "verify-email")] == original_alice,
                       "Unapproved resend generated a token.")
            # This timestamp manipulation is ONLY on the marked disposable DB,
            # to exercise the >60s cooldown without sleeping in production.
            with owner.begin() as conn:
                conn.execute(text("""
                    UPDATE account_email_challenges SET
                        created_at=clock_timestamp()-interval '90 seconds'
                    WHERE token_sha256=:digest
                """), {"digest": __import__("hashlib").sha256(
                    original_alice.encode("ascii"),
                ).hexdigest()})
            accepted = alice.post(
                "/api/session/resend-confirmation", headers=origin,
                json={"email": emails[0], "password": old_password},
            )
            _check(accepted.status_code == 202, "Password-verified resend failed.")
            replacement = delivered[(emails[0], "verify-email")]
            _check(replacement != original_alice, "Resend reused old token.")
            _check(alice.post(
                "/api/session/verify-email", headers=origin,
                json={"token": original_alice},
            ).status_code == 400, "Old signup token survived resend rotation.")
            with owner.connect() as conn:
                _check(conn.scalar(text("""
                    SELECT count(*) FROM account_email_challenges
                    WHERE purpose='signup' AND consumed_at IS NULL
                """)) == 2, "Resend created multiple active signup challenges.")
            _check(alice.post(
                "/api/session/register", headers=origin,
                json={
                    "email": emails[0], "password": old_password,
                    "organization_name": "Unexpected Duplicate",
                },
            ).status_code == 202, "Existing email was revealed.")
            with owner.connect() as conn:
                _check(conn.scalar(text("SELECT count(*) FROM users")) == 2,
                       "Registration did not COMMIT across connections.")
                _check(conn.scalar(text("SELECT count(*) FROM organizations")) == 0,
                       "Unverified registration created a tenant.")

            orgs: list[str] = []
            for client, email in ((alice, emails[0]), (bob, emails[1])):
                token = delivered[(email, "verify-email")]
                answer = client.post(
                    "/api/session/verify-email", headers=origin,
                    json={"token": token},
                )
                _check(answer.status_code == 200,
                       f"HTTP verification failure: {answer.status_code}")
                orgs.append(answer.json()["organization_id"])
                _check(client.post(
                    "/api/session/verify-email", headers=origin,
                    json={"token": token},
                ).status_code == 400, "Consumed email token accepted.")
            _check(orgs[0] != orgs[1], "Two registrations reused organization ID.")
            with owner.connect() as conn:
                _check(conn.scalar(text("""
                    SELECT count(*) FROM organization_memberships
                    WHERE role='owner' AND status='active'
                """)) == 2, "Verified users have no organizations/roles.")

            for client, email in ((alice, emails[0]), (bob, emails[1])):
                login = client.post(
                    "/api/session/login", headers=origin,
                    json={"email": email, "password": old_password},
                )
                _check(login.status_code == 200, "Verified user cannot login.")
                _check("Secure" in login.headers["set-cookie"],
                       "HTTP login missed Secure session cookie.")
                _check(client.get("/api/session/me").status_code == 200,
                       "Committed session missing from physical tenant connection.")
                visible = client.get("/api/session/organizations")
                _check(visible.status_code == 200,
                       "Authenticated organization list endpoint failed.")
                organizations = visible.json()["organizations"]
                _check(len(organizations) == 1 and
                       organizations[0]["id"] == orgs[0 if client is alice else 1] and
                       organizations[0]["role"] == "owner",
                       "Membership list returned other user's tenant.")

            alice_csrf = alice.post(
                "/api/session/login", headers=origin,
                json={"email": emails[0], "password": old_password},
            ).json()["csrf_token"]
            own = f"/api/session/organizations/{orgs[0]}"
            foreign = f"/api/session/organizations/{orgs[1]}"
            extract_url = f"{own}/requests/extract"
            request_body = {
                "text": valid_llm_text,
                "consent_to_external_processing": True,
            }
            _check(alice.post(
                extract_url, headers=origin,
                json=request_body,
            ).status_code == 401,
                   "External provider was available without CSRF.")
            _check(alice.post(
                extract_url, headers={**origin, "X-CSRF-Token": alice_csrf},
                json={**request_body, "consent_to_external_processing": False},
            ).status_code == 422, "Extraction allowed without explicit consent.")
            _check(alice.post(
                f"{foreign}/requests/extract",
                headers={**origin, "X-CSRF-Token": alice_csrf},
                json=request_body,
            ).status_code == 403, "Foreign organization request leaked to Groq.")
            _check(not groq_calls,
                   "Preauthorization request unexpectedly sent customer text.")
            extracted_response = alice.post(
                extract_url,
                headers={**origin, "X-CSRF-Token": alice_csrf},
                json=request_body,
            )
            _check(extracted_response.status_code == 200,
                   f"Authorized extraction returned HTTP {extracted_response.status_code}")
            preview = extracted_response.json()
            _check(
                len(preview["extracted_request"]["lines"]) == 2
                and preview["requires_human_review"] is True
                and preview["approved"] is False
                and len(groq_calls) == 1,
                "LLM extraction failed review-only two-line preview contract.",
            )
            _check("price_rub" not in preview["extracted_request"]["lines"][0],
                   "LLM assigned an invented price.")
            with owner.connect() as conn:
                _check(conn.scalar(text(
                    "SELECT count(*) FROM quote_documents"
                )) == 0, "LLM preview persisted quote without human approval.")
            fabricated_quote = True
            bad_evidence = alice.post(
                extract_url,
                headers={**origin, "X-CSRF-Token": alice_csrf},
                json=request_body,
            )
            _check(
                bad_evidence.status_code == 502
                and bad_evidence.json()["code"] == "invalid_output",
                "LLM hallucinated source evidence was accepted.",
            )
            fabricated_quote = False
            # Atomically bound quotas on real restricted tenant LOGIN: a second
            # concurrent inference for the same signed-in user is forbidden.
            session_value = alice.cookies.get(SESSION_COOKIE)
            _check(isinstance(session_value, str),
                   "A20 missing actual HttpOnly authenticated browser cookie.")
            held = reserve_inference(
                tenant, session_cookie=session_value, csrf=alice_csrf,
                organization_id=uuid.UUID(orgs[0]), model="nvidia/nemotron-3-ultra-550b-a55b:free",
                input_chars=len(valid_llm_text),
            )
            try:
                try:
                    reserve_inference(
                        tenant, session_cookie=session_value, csrf=alice_csrf,
                        organization_id=uuid.UUID(orgs[0]), model="nvidia/nemotron-3-ultra-550b-a55b:free",
                        input_chars=len(valid_llm_text),
                    )
                except InferenceQuotaExceeded as error:
                    _check(error.reason == "user_busy",
                           "Concurrent inference was not atomically blocked.")
                else:
                    raise RuntimeError("A20 allowed a concurrent user inference.")
            finally:
                finish_inference(
                    tenant, request_id=held, session_cookie=session_value,
                    succeeded=False,
                )
            for _ in range(3):
                accepted_llm = alice.post(
                    extract_url,
                    headers={**origin, "X-CSRF-Token": alice_csrf},
                    json=request_body,
                )
                _check(accepted_llm.status_code == 200,
                       "A20 blocked a permitted call below the user quota.")
            before_denied = len(groq_calls)
            _check(alice.post(
                extract_url,
                headers={**origin, "X-CSRF-Token": alice_csrf},
                json=request_body,
            ).status_code == 429,
                   "A20 allowed inference after the 6/hour user quota.")
            _check(len(groq_calls) == before_denied,
                   "Quota-exhausted request reached the external provider.")
            with owner.connect() as conn:
                attempts = conn.execute(text("""
                    SELECT count(*),count(*) FILTER(WHERE status='failed')
                    FROM tenant_llm_attempts WHERE organization_id=:org
                """), {"org": orgs[0]}).one()
                _check(attempts == (6, 2),
                       "A20 inference failures were excluded from quota.")
            _check(TestClient(app, base_url="https://testserver").get(
                "/api/session/organizations"
            ).status_code == 401, "Anonymous organization enumeration allowed.")
            bob_csrf = bob.post(
                "/api/session/login", headers=origin,
                json={"email": emails[1], "password": old_password},
            ).json()["csrf_token"]
            # A20 organization/day controls are distinct from the user's
            # 1-hour quota. Synthetic, older-than-one-hour attempts live ONLY
            # in this isolated clone and are removed with that clone.
            with owner.begin() as conn:
                bob_identity = conn.scalar(text("""
                    SELECT id FROM users WHERE email=:email
                """), {"email": emails[1]})
                _check(bob_identity is not None, "A20 missing test user.")
                conn.execute(text("""
                    INSERT INTO tenant_llm_attempts(
                        id, organization_id, user_id, model, input_chars,
                        status, created_at, lease_expires_at, finished_at
                    )
                    SELECT gen_random_uuid(),:org,:person,'openai/gpt-oss-120b',
                           12000,'success',
                           clock_timestamp()-interval '2 hours',
                           clock_timestamp()-interval '119 minutes',
                           clock_timestamp()-interval '119 minutes'
                    FROM generate_series(1,10)
                """), {"org": uuid.UUID(orgs[1]), "person": bob_identity})
            existing_calls = len(groq_calls)
            own_bob_extract = f"/api/session/organizations/{orgs[1]}/requests/extract"
            _check(bob.post(
                own_bob_extract,
                headers={**origin, "X-CSRF-Token": bob_csrf},
                json=request_body,
            ).status_code == 429, "Daily organization character ceiling failed.")
            _check(len(groq_calls) == existing_calls,
                   "Organization text budget exhausted but provider called.")
            with owner.begin() as conn:
                conn.execute(text("""
                    UPDATE tenant_llm_attempts SET input_chars=1
                    WHERE organization_id=:org
                """), {"org": uuid.UUID(orgs[1])})
                conn.execute(text("""
                    INSERT INTO tenant_llm_attempts(
                        id, organization_id, user_id, model, input_chars,
                        status, created_at, lease_expires_at, finished_at
                    )
                    SELECT gen_random_uuid(),:org,:person,'openai/gpt-oss-120b',
                           1,'success',
                           clock_timestamp()-interval '2 hours',
                           clock_timestamp()-interval '119 minutes',
                           clock_timestamp()-interval '119 minutes'
                    FROM generate_series(1,30)
                """), {"org": uuid.UUID(orgs[1]), "person": bob_identity})
            _check(bob.post(
                own_bob_extract,
                headers={**origin, "X-CSRF-Token": bob_csrf},
                json=request_body,
            ).status_code == 429,
                   "Daily organization 40-call limit failed.")
            _check(len(groq_calls) == existing_calls,
                   "Organization call quota was bypassed.")
            outsider = bob.post(
                f"/api/session/organizations/{orgs[0]}/invitations",
                headers={**origin, "X-CSRF-Token": bob_csrf},
                json={"email": emails[0], "role": "editor"},
            )
            _check(outsider.status_code == 403,
                   "Non-owner issued an invitation to foreign organization.")
            invitation = alice.post(
                f"/api/session/organizations/{orgs[0]}/invitations",
                headers={**origin, "X-CSRF-Token": alice_csrf},
                json={"email": emails[1], "role": "viewer"},
            )
            _check(invitation.status_code == 201, "Owner invite failed.")
            _check(alice.post(
                f"/api/session/organizations/{orgs[0]}/invitations",
                headers={**origin, "X-CSRF-Token": alice_csrf},
                json={"email": "not-registered@a17.example", "role": "editor"},
            ).status_code == 403, "Unknown user received an invitation.")
            invite_id = invitation.json()["invitation_id"]
            _check(len(bob.get("/api/session/invitations").json()) == 1,
                   "Invite not visible to its authenticated recipient.")
            _check(alice.post(
                f"/api/session/invitations/{invite_id}/accept",
                headers={**origin, "X-CSRF-Token": alice_csrf},
            ).status_code == 403, "Invitation accepted by wrong user.")
            _check(bob.post(
                f"/api/session/invitations/{invite_id}/accept",
                headers={**origin, "X-CSRF-Token": bob_csrf},
            ).status_code == 204, "Invitation not accepted.")
            _check(bob.post(
                f"/api/session/invitations/{invite_id}/accept",
                headers={**origin, "X-CSRF-Token": bob_csrf},
            ).status_code == 403, "Invitation replay unexpectedly succeeded.")
            _check(len(bob.get("/api/session/organizations").json()["organizations"]) == 2,
                   "Accepted organization missing from recipient's list.")
            _check(bob.get(
                f"/api/session/organizations/{orgs[0]}/members"
            ).status_code == 403, "Viewer enumerated organization memberships.")
            members = alice.get(
                f"/api/session/organizations/{orgs[0]}/members"
            )
            _check(members.status_code == 200 and len(members.json()) == 2,
                   "Owner cannot review team members.")
            invited_user_id = next(
                member["user_id"] for member in members.json()
                if member["role"] == "viewer"
            )
            own = f"/api/session/organizations/{orgs[0]}"
            foreign = f"/api/session/organizations/{orgs[1]}"
            prior_llm_calls = len(groq_calls)
            _check(bob.post(
                extract_url, headers={**origin, "X-CSRF-Token": bob_csrf},
                json=request_body,
            ).status_code == 403,
                   "Viewer accessed provider-backed tenant extraction.")
            _check(len(groq_calls) == prior_llm_calls,
                   "Viewer's customer text leaked to external provider.")
            _check(bob.post(
                f"{own}/catalog/import",
                headers={**origin, "X-CSRF-Token": bob_csrf,
                         "Idempotency-Key": "viewer-cannot-import"},
                files={"file": ("catalog.csv", _csv("429.90"), "text/csv")},
            ).status_code == 403, "Viewer obtained catalog write rights.")

            _check(alice.get(f"{foreign}/catalog/current").status_code == 403,
                   "Alice can access Bob tenant catalog.")

            _check(alice.post(
                f"{own}/catalog/import",
                headers={**origin, "Idempotency-Key": "csrf-negative"},
                files={"file": ("denied.csv", _csv("429.90"), "text/csv")},
            ).status_code == 401, "Tenant import accepted without CSRF.")
            imported = alice.post(
                f"{own}/catalog/import",
                headers={**origin, "X-CSRF-Token": alice_csrf,
                         "Idempotency-Key": "real-a15-catalog"},
                files={"file": ("catalog.csv", _csv("429.90"), "text/csv")},
            )
            _check(imported.status_code == 201,
                   f"Live 0015 HTTP catalog import failed: {imported.status_code}")
            version = imported.json()["version_id"]
            with owner.connect() as conn:
                _check(conn.scalar(text("""
                    SELECT count(*) FROM catalog_items
                    WHERE organization_id=:org AND catalog_version_id=:version
                """), {"org": orgs[0], "version": version}) == 1,
                       "Catalog import did not persist transaction.")

            # A21: the LLM preview remains unapproved until an actual
            # session-bound reviewer commits the exact verified two lines.
            _check("extraction_id" in preview,
                   "Groq preview has no durable server provenance.")
            reviewed_quote = {
                "catalog_version_id": version,
                "extracted_request": preview["extracted_request"],
                "review": {
                    "extraction_id": preview["extraction_id"],
                    "source_text": valid_llm_text,
                    "original_extracted_request": preview["extracted_request"],
                    "reviewed_lines": [True, True],
                },
            }
            refused_review = alice.post(
                f"{own}/quotes",
                headers={**origin, "X-CSRF-Token": alice_csrf,
                         "Idempotency-Key": "a21-not-reviewed"},
                json={**reviewed_quote, "review": {
                    **reviewed_quote["review"],
                    "reviewed_lines": [True, False],
                }},
            )
            _check(refused_review.status_code == 409,
                   "Invalid review HTTP " + str(refused_review.status_code)
                   + " response " + str(refused_review.json()))
            false_source = alice.post(
                f"{own}/quotes",
                headers={**origin, "X-CSRF-Token": alice_csrf,
                         "Idempotency-Key": "a21-fake-source"},
                json={**reviewed_quote, "review": {
                    **reviewed_quote["review"],
                    "source_text": "This text does not contain the two quotes",
                }},
            )
            _check(false_source.status_code == 409,
                   "Server accepted a false source binding.")
            with owner.connect() as conn:
                _check(conn.scalar(text("SELECT count(*) FROM quote_documents")) == 0,
                       "Invalid A21 review did not roll back quote transaction.")
            audited = alice.post(
                f"{own}/quotes",
                headers={**origin, "X-CSRF-Token": alice_csrf,
                         "Idempotency-Key": "a21-reviewed-quote"},
                json=reviewed_quote,
            )
            _check(audited.status_code == 201,
                   f"A21 quote review transaction failed HTTP {audited.status_code}")
            _check(len(audited.json()["lines"]) == 2,
                   "A21 audited quote lost the extracted rows.")
            reviewed_id = audited.json()["quote_id"]
            recorded = alice.get(f"{own}/quotes/{reviewed_id}/reviews/1")
            _check(recorded.status_code == 200,
                   "Manager attestation not readable after COMMIT.")
            _check(len(recorded.json()["reviewed_line_hashes"]) == 2
                   and recorded.json()["extraction_id"] == preview["extraction_id"],
                   "A21 audit not bound to original LLM preview and lines.")
            with owner.begin() as conn:
                _check(conn.scalar(text("""
                    SELECT count(*) FROM tenant_quote_reviews
                    WHERE organization_id=:org AND quote_id=:quote
                """), {"org": uuid.UUID(orgs[0]),
                       "quote": uuid.UUID(reviewed_id)}) == 1,
                       "A21 review was not committed atomically.")
                savepoint = conn.begin_nested()
                try:
                    conn.execute(text("""
                        UPDATE public.tenant_quote_reviews
                        SET reviewed_at=clock_timestamp()
                        WHERE organization_id=:org AND quote_id=:quote
                    """), {"org": uuid.UUID(orgs[0]),
                           "quote": uuid.UUID(reviewed_id)})
                except SQLAlchemyError:
                    savepoint.rollback()
                else:
                    savepoint.rollback()
                    raise RuntimeError("Immutable review audit allowed UPDATE.")
            _check(alice.get(
                f"{own}/quotes/{reviewed_id}/reviews/1"
            ).status_code == 200, "Immutability probe damaged review attestation.")
            _check(alice.post(
                f"{own}/quotes",
                headers={**origin, "X-CSRF-Token": alice_csrf,
                         "Idempotency-Key": "a21-reviewed-quote"},
                json=reviewed_quote,
            ).status_code == 201, "A21 idempotent retry failed.")
            before_attempt = alice.get(f"{own}/quotes").json()["items"]
            _check(alice.post(
                f"{own}/quotes",
                headers={**origin, "X-CSRF-Token": alice_csrf,
                         "Idempotency-Key": "a21-reused-extraction"},
                json=reviewed_quote,
            ).status_code == 409,
                   "Same inference proof reattached to a different quote.")
            _check(len(alice.get(f"{own}/quotes").json()["items"]) ==
                   len(before_attempt), "Duplicate evidence left a phantom quote.")
            _check(alice.get(
                f"{foreign}/quotes/{reviewed_id}/reviews/1"
            ).status_code == 403, "Foreign organization accessed private review.")

            request = {
                "catalog_version_id": version,
                "extracted_request": {"lines": [{
                    "source_quote": "Office paper A4 2 packs",
                    "description": "Office paper A4 80",
                    "category": "paper", "quantity": 2, "unit": "пачка",
                    "brand": None, "paper_format": "A4", "paper_gsm": 80,
                    "pen_ink_color": None, "pen_ink_type": None, "attributes": {},
                }]},
            }
            result = alice.post(
                f"{own}/quotes",
                headers={**origin, "X-CSRF-Token": alice_csrf,
                         "Idempotency-Key": "real-a15-quote"},
                json=request,
            )
            _check(result.status_code == 201,
                   f"Live tenant quote HTTP create failed: {result.status_code}")
            _check(result.json()["total_rub"] == "859.80",
                   "Historical quote total changed.")
            quote_id = result.json()["quote_id"]
            multi = {
                "catalog_version_id": version,
                "extracted_request": {"lines": [
                    request["extracted_request"]["lines"][0],
                    {
                        **request["extracted_request"]["lines"][0],
                        "source_quote": "Second pack of A4 paper",
                        "quantity": 1,
                    },
                ]},
            }
            multi_result = alice.post(
                f"{own}/quotes",
                headers={**origin, "X-CSRF-Token": alice_csrf,
                         "Idempotency-Key": "a18-multiline"},
                json=multi,
            )
            _check(multi_result.status_code == 201
                   and multi_result.json()["total_rub"] == "1289.70"
                   and len(multi_result.json()["lines"]) == 2,
                   "Two-line quote not calculated from immutable catalog prices.")
            multi_id = multi_result.json()["quote_id"]
            multi_pdf = alice.get(f"{own}/quotes/{multi_id}/pdf")
            _check(multi_pdf.status_code == 200
                   and multi_pdf.content.startswith(b"%PDF-"),
                   "Multiple-line quote PDF was not rendered.")
            revise = alice.post(
                f"{own}/quotes/{multi_id}/revisions",
                headers={**origin, "X-CSRF-Token": alice_csrf,
                         "Idempotency-Key": "a18-multiline-revise"},
                json={**multi, "expected_revision": 1},
            )
            _check(revise.status_code == 201
                   and revise.json()["revision"] == 2
                   and revise.json()["total_rub"] == "1289.70",
                   "Multi-line immutable quote revision failed.")
            _check(alice.post(
                f"{own}/quotes/{multi_id}/revisions",
                headers={**origin, "X-CSRF-Token": alice_csrf,
                         "Idempotency-Key": "a18-stale-multiline"},
                json={**multi, "expected_revision": 1},
            ).status_code == 409, "Stale multi-line revision overwrote history.")
            _check(alice.get(
                f"{own}/quotes/{multi_id}?revision=1"
            ).json()["total_rub"] == "1289.70",
                   "Original immutable multi-line quote snapshot was mutated.")
            alice_history = alice.get(f"{own}/quotes")
            _check(alice_history.status_code == 200
                   and any(item["quote_id"] == quote_id
                           for item in alice_history.json()["items"]),
                   "Tenant quote history omitted committed revision.")
            _check(bob.get(f"{own}/quotes").status_code == 200,
                   "Accepted viewer could not list tenant quote history.")
            _check(bob.get(f"{own}/quotes/{quote_id}").status_code == 200,
                   "Accepted viewer cannot read organization's quote.")
            owner_id = next(
                member["user_id"] for member in members.json()
                if member["role"] == "owner"
            )
            _check(alice.delete(
                f"{own}/members/{owner_id}",
                headers={**origin, "X-CSRF-Token": alice_csrf},
            ).status_code == 403, "Organization owner revoked herself.")
            changed = alice.patch(
                f"{own}/members/{invited_user_id}",
                headers={**origin, "X-CSRF-Token": alice_csrf},
                json={"role": "editor"},
            )
            _check(changed.status_code == 204, "Owner could not promote editor.")
            editor_quote = bob.post(
                f"{own}/quotes",
                headers={**origin, "X-CSRF-Token": bob_csrf,
                         "Idempotency-Key": "editor-a17-quote"},
                json=request,
            )
            _check(editor_quote.status_code == 201,
                   "Promoted editor could not write an own organization quote.")
            _check(bob.delete(
                f"{own}/members/{invited_user_id}",
                headers={**origin, "X-CSRF-Token": bob_csrf},
            ).status_code == 403, "Editor revoked own team membership.")
            _check(alice.delete(
                f"{own}/members/{invited_user_id}",
                headers={**origin, "X-CSRF-Token": alice_csrf},
            ).status_code == 204, "Owner failed to revoke editor.")
            _check(bob.get(f"{own}/quotes/{quote_id}").status_code == 403,
                   "Revoked member retained read rights.")
            _check(bob.post(
                f"{own}/quotes",
                headers={**origin, "X-CSRF-Token": bob_csrf,
                         "Idempotency-Key": "revoked-cannot-quote"},
                json=request,
            ).status_code == 403, "Revoked member retained write rights.")

            # A18: a verified owner can onboard a never-registered colleague
            # without fabricating a separate organization for that person.
            newcomer = "charlie@a18.example"
            transient_mail_failure.add(newcomer)
            invited = alice.post(
                f"{own}/invitations/email",
                headers={**origin, "X-CSRF-Token": alice_csrf},
                json={"email": newcomer, "role": "viewer"},
            )
            _check(invited.status_code == 202, "Owner cannot invite new email.")
            with owner.begin() as conn:
                _check(conn.scalar(text("""
                    SELECT count(*) FROM organization_email_invitations
                    WHERE email=:email AND status='pending' AND attempts=1
                """), {"email": newcomer}) == 1,
                       "Transient team delivery was incorrectly marked sent.")
                conn.execute(text("""
                    UPDATE organization_email_invitations
                    SET next_attempt_at=clock_timestamp()-interval '1 second'
                    WHERE email=:email
                """), {"email": newcomer})
            retry_invite = asyncio.run(locally_team_deliver(auth))
            _check(retry_invite == "sent", "Team invitation retry did not deliver.")
            join_token = delivered.get((newcomer, "join"))
            _check(isinstance(join_token, str),
                   "New-account invitation did not pass encrypted outbox mailer.")
            _check(alice.post(
                f"{own}/invitations/email",
                headers={**origin, "X-CSRF-Token": alice_csrf},
                json={"email": newcomer, "role": "editor"},
            ).status_code == 202, "Duplicate invite leaked account existence.")
            _check(alice.post(
                "/api/session/invitations/register", headers=origin,
                json={"token": "x" * 43, "password": old_password},
            ).status_code == 400, "Forged join token accepted.")
            with owner.connect() as conn:
                _check(conn.scalar(text("""
                    SELECT count(*) FROM users WHERE email=:email
                """), {"email": newcomer}) == 0,
                "Issuing invitation fabricated account before proof of inbox.")
            with TestClient(
                app, base_url="https://testserver", client=("127.0.0.3", 50224),
            ) as charlie:
                joined = charlie.post(
                    "/api/session/invitations/register", headers=origin,
                    json={"token": join_token, "password": old_password},
                )
                _check(joined.status_code == 200,
                       "New invitee could not create verified user and join.")
                _check(joined.json()["organization_id"] == orgs[0],
                       "Invitee joined an unrelated or new organization.")
                _check(charlie.post(
                    "/api/session/invitations/register", headers=origin,
                    json={"token": join_token, "password": old_password},
                ).status_code == 400, "Consumed join token was replayed.")
                confirmed = charlie.post(
                    "/api/session/login", headers=origin,
                    json={"email": newcomer, "password": old_password},
                )
                _check(confirmed.status_code == 200,
                       "Invited verified account cannot authenticate.")
                visible_orgs = charlie.get("/api/session/organizations").json()
                _check(visible_orgs["organizations"] == [
                    {"id": orgs[0], "name": "Acme Test Supplies", "role": "viewer"},
                ], "Invited user was given wrong role or an extra organization.")
                _check(charlie.get(f"{own}/quotes/{quote_id}").status_code == 200,
                       "New viewer cannot read shared organization quote.")
                charlie_csrf = confirmed.json()["csrf_token"]
                _check(charlie.post(
                    f"{own}/quotes",
                    headers={**origin, "X-CSRF-Token": charlie_csrf,
                             "Idempotency-Key": "new-viewer-no-quote"},
                    json=request,
                ).status_code == 403, "New viewer obtained write permission.")
            with owner.connect() as conn:
                _check(conn.scalar(text("SELECT count(*) FROM organizations")) == 2,
                       "Invitee registration created unwanted separate organization.")
                _check(conn.scalar(text("""
                    SELECT count(*) FROM users WHERE email=:email
                    AND status='active' AND email_verified_at IS NOT NULL
                """), {"email": newcomer}) == 1,
                       "Unverified invitee or duplicate account inserted.")

            forgot = alice.post(
                "/api/session/password/forgot", headers=origin,
                json={"email": emails[0]},
            )
            _check(forgot.status_code == 202, "Password recovery request denied.")
            reset_token = delivered.get((emails[0], "reset-password"))
            _check(isinstance(reset_token, str),
                   "Password reset email wasn't delivered through mock Resend.")
            changed = alice.post(
                "/api/session/password/reset", headers=origin,
                json={"token": reset_token, "password": "A15UpdatedPassword!2026"},
            )
            _check(changed.status_code == 204, "Password reset failed.")
            _check(alice.get("/api/session/me").status_code == 401,
                   "Password reset did not revoke prior sessions.")
            _check(alice.post(
                "/api/session/password/reset", headers=origin,
                json={"token": reset_token, "password": old_password},
            ).status_code == 400, "Reset token replay succeeded.")
            _check(alice.post(
                "/api/session/login", headers=origin,
                json={"email": emails[0], "password": old_password},
            ).status_code == 401, "Old password still authenticates.")
            _check(alice.post(
                "/api/session/login", headers=origin,
                json={"email": emails[0], "password": "A15UpdatedPassword!2026"},
            ).status_code == 200, "Updated password can't authenticate.")
            _check(alice.get(f"{own}/quotes/{quote_id}").status_code == 200,
                   "Password reset lost immutable quote history.")
    finally:
        app.dependency_overrides.pop(get_account_email_dispatcher, None)
        app.dependency_overrides.pop(get_team_email_dispatcher, None)


def run() -> None:
    cfg = dotenv_values(_ROOT / ".env")
    owner_password = cfg.get("KP_DESK_DB_PASSWORD")
    if not isinstance(owner_password, str) or len(owner_password) < 32:
        raise RuntimeError("PostgreSQL owner credentials not configured.")
    port = int(cfg.get("KP_DESK_DB_PORT") or 55432)
    accounts = json.loads((_ROOT / "credentials" / "kpdesk-runtime-db.json").read_text(
        encoding="utf-8",
    ))
    nonce = secrets.token_hex(6)
    database = "kpdesk_accounts_http_" + nonce
    marker = "kpdesk:accounts-http:" + nonce
    temp_file = _ROOT / "backups" / "2026-10-10-a15-http" / (nonce + ".dump")
    temp_file.parent.mkdir(parents=True, exist_ok=True)
    live = _connection("kpdesk", "kpdesk", owner_password, port)
    admin = _connection("postgres", "kpdesk", owner_password, port, admin=True)
    clone_owner: Engine | None = None
    created = False
    auth: Engine | None = None
    tenant: Engine | None = None
    try:
        with live.connect() as conn:
            _check(
                conn.scalar(text("SELECT version_num FROM alembic_version"))
                in ("0015_verified_accounts", "0016_account_resend_orgs",
                    "0017_member_invitations", "0018_email_team_onboarding",
                    "0019_tenant_llm_budget", "0020_quote_review_audit", "0021_supplier_precision_reviews", "0022_supplier_mapping_profiles", "0023_supplier_mapping_history", "0024_supplier_unit_reviews", "0025_request_candidate_audit", "0026_request_source_restrictions", "0027_candidate_adjudications"),
                "Live account baseline not found.")
            _check(conn.scalar(text("SELECT count(*) FROM users")) == 0,
                   "Refusing to copy nonempty sensitive working DB.")
        with temp_file.open("xb") as stream:
            subprocess.run(
                ["docker", "exec", "kpdesk-db-1", "pg_dump",
                 "-U", "kpdesk", "-d", "kpdesk", "-Fc",
                 "--no-owner", "--serializable-deferrable"],
                stdout=stream, stderr=subprocess.PIPE,
                check=True, timeout=120,
            )
        with admin.connect() as conn:
            _check(conn.scalar(text("""
                SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=:name)
            """), {"name": database}) is False,
                   "Temporary DB name collision.")
            conn.exec_driver_sql(f"CREATE DATABASE {database} TEMPLATE template0")
            created = True
            conn.exec_driver_sql(
                f"COMMENT ON DATABASE {database} IS '{marker}'"
            )
        _docker_input(
            "pg_restore", "-U", "kpdesk", "--dbname=" + database,
            "--single-transaction", "--exit-on-error", "--no-owner",
            input_file=temp_file,
        )
        clone_owner = _connection(database, "kpdesk", owner_password, port)
        with clone_owner.connect() as conn:
            clone_revision = conn.scalar(text("SELECT version_num FROM alembic_version"))
        if clone_revision == "0015_verified_accounts":
            with clone_owner.begin() as conn:
                with Operations.context(MigrationContext.configure(conn)):
                    importlib.import_module(
                        "migrations.versions.0016_account_resend_orgs"
                    ).upgrade()
                conn.execute(text("""
                    UPDATE alembic_version SET version_num='0016_account_resend_orgs'
                """))
        elif clone_revision not in (
            "0016_account_resend_orgs", "0017_member_invitations",
            "0018_email_team_onboarding", "0019_tenant_llm_budget",
            "0020_quote_review_audit", "0021_supplier_precision_reviews", "0022_supplier_mapping_profiles", "0023_supplier_mapping_history", "0024_supplier_unit_reviews", "0025_request_candidate_audit", "0026_request_source_restrictions", "0027_candidate_adjudications",
        ):
            raise RuntimeError("Unexpected restored A16 clone revision.")
        with clone_owner.connect() as conn:
            version_after_16 = conn.scalar(
                text("SELECT version_num FROM alembic_version")
            )
        if version_after_16 == "0016_account_resend_orgs":
            with clone_owner.begin() as conn:
                with Operations.context(MigrationContext.configure(conn)):
                    importlib.import_module(
                        "migrations.versions.0017_member_invitations"
                    ).upgrade()
                conn.execute(text("""
                    UPDATE alembic_version SET version_num='0017_member_invitations'
                """))
        elif version_after_16 not in (
            "0017_member_invitations", "0018_email_team_onboarding",
            "0019_tenant_llm_budget", "0020_quote_review_audit", "0021_supplier_precision_reviews", "0022_supplier_mapping_profiles", "0023_supplier_mapping_history", "0024_supplier_unit_reviews", "0025_request_candidate_audit", "0026_request_source_restrictions", "0027_candidate_adjudications",
        ):
            raise RuntimeError("Unexpected A17 rehearsal database revision.")
        with clone_owner.connect() as conn:
            version_after_17 = conn.scalar(
                text("SELECT version_num FROM alembic_version")
            )
        if version_after_17 == "0017_member_invitations":
            with clone_owner.begin() as conn:
                with Operations.context(MigrationContext.configure(conn)):
                    importlib.import_module(
                        "migrations.versions.0018_email_team_onboarding"
                    ).upgrade()
                conn.execute(text("""
                    UPDATE alembic_version SET version_num='0018_email_team_onboarding'
                """))
        elif version_after_17 not in (
            "0018_email_team_onboarding", "0019_tenant_llm_budget",
            "0020_quote_review_audit", "0021_supplier_precision_reviews", "0022_supplier_mapping_profiles", "0023_supplier_mapping_history", "0024_supplier_unit_reviews", "0025_request_candidate_audit", "0026_request_source_restrictions", "0027_candidate_adjudications",
        ):
            raise RuntimeError("Unexpected A18 invitation clone revision.")
        with clone_owner.connect() as conn:
            version_after_18 = conn.scalar(
                text("SELECT version_num FROM alembic_version")
            )
        if version_after_18 == "0018_email_team_onboarding":
            with clone_owner.begin() as conn:
                with Operations.context(MigrationContext.configure(conn)):
                    importlib.import_module(
                        "migrations.versions.0019_tenant_llm_budget"
                    ).upgrade()
                conn.execute(text("""
                    UPDATE alembic_version SET version_num='0019_tenant_llm_budget'
                """))
        elif version_after_18 not in (
            "0019_tenant_llm_budget", "0020_quote_review_audit", "0021_supplier_precision_reviews", "0022_supplier_mapping_profiles", "0023_supplier_mapping_history", "0024_supplier_unit_reviews", "0025_request_candidate_audit", "0026_request_source_restrictions", "0027_candidate_adjudications",
        ):
            raise RuntimeError("Unexpected A20 quota clone revision.")
        with clone_owner.connect() as conn:
            version_after_19 = conn.scalar(
                text("SELECT version_num FROM alembic_version")
            )
        if version_after_19 == "0019_tenant_llm_budget":
            with clone_owner.begin() as conn:
                with Operations.context(MigrationContext.configure(conn)):
                    importlib.import_module(
                        "migrations.versions.0020_quote_review_audit"
                    ).upgrade()
                conn.execute(text("""
                    UPDATE alembic_version SET version_num='0020_quote_review_audit'
                """))
        elif version_after_19 not in (
            "0020_quote_review_audit", "0021_supplier_precision_reviews", "0022_supplier_mapping_profiles", "0023_supplier_mapping_history", "0024_supplier_unit_reviews", "0025_request_candidate_audit", "0026_request_source_restrictions", "0027_candidate_adjudications",
        ):
            raise RuntimeError("Unexpected A21/A22 review audit revision.")
        auth = _connection(
            database, "kpdesk_auth_service", accounts["kpdesk_auth_service"],
            port, runtime="kpdesk_auth_runtime",
        )
        tenant = _connection(
            database, "kpdesk_tenant_service", accounts["kpdesk_tenant_service"],
            port, runtime="kpdesk_tenant_runtime",
        )
        with auth.begin() as conn:
            _check(conn.scalar(text("SELECT current_user")) == "kpdesk_auth_runtime",
                   "Auth service wrong PostgreSQL identity.")
        with tenant.begin() as conn:
            _check(conn.scalar(text("SELECT current_user")) == "kpdesk_tenant_runtime",
                   "Tenant service wrong PostgreSQL identity.")
        app = create_app(testing=True, tenant_sessions=True)
        verify_http(app, clone_owner, auth, tenant)
        with live.connect() as conn:
            _check(conn.scalar(text("SELECT count(*) FROM users")) == 0,
                   "E2E unexpectedly modified working DB users.")
        print("A21_IMMUTABLE_QUOTE_REVIEW_HTTP_PASSED "
              "source_bound=true reviewer_scoped=true "
              "one_preview_one_quote=true invalid_review_rollback=true "
              "quota_atomic=true user_hour_max_six=true "
              "concurrent_same_user_denied=true failed_inference_counted=true "
              "groq_preview_only=true consent_required=true "
              "authorization_before_provider=true forged_source_rejected=true "
              "viewer_llm_denied=true quote_unmodified_by_inference=true "
              "unregistered_verified_email_join=true invitation_outbox_retry=true "
              "one_use_invite_token=true no_extra_org=true "
              "password_verified_resend=true cooldown=true old_token_revoked=true "
              "organization_visibility_scoped=true verified_user_invites=true "
              "role_promotion=true revoked_access_denied=true "
              "registration=true email_mock_resend=true outbox_retry=true "
              "hashed_one_time_tokens=true "
              "two_organizations=true real_catalog_quote=true "
              "cross_tenant_rls=true reset_revoked_sessions=true "
              "production_users_unchanged=true")
    finally:
        for engine in (auth, tenant, clone_owner):
            if engine is not None:
                engine.dispose()
        try:
            if created:
                with admin.connect() as conn:
                    current = conn.scalar(text("""
                        SELECT shobj_description(oid,'pg_database')
                        FROM pg_database WHERE datname=:name
                    """), {"name": database})
                    if current != marker:
                        raise RuntimeError("Refusing cleanup of unmarked E2E database.")
                    conn.exec_driver_sql(f"DROP DATABASE {database} WITH (FORCE)")
        finally:
            admin.dispose()
            live.dispose()
            if temp_file.exists():
                temp_file.unlink()


if __name__ == "__main__":
    run()
