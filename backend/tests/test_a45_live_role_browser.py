"""A45: real separated PostgreSQL LOGINs, current live-release schema and HTTPS UI.

Creates and destroys a provenance-tagged PostgreSQL clone. No live database writes.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import secrets
import subprocess
import uuid
from collections.abc import AsyncIterator
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from canonical_a96_multicategory_journey import verify_full_taxonomy_quote_journey
from canonical_checkpoint_acceptance import verify_checkpoint_clone_contract
from canonical_checkpoint_benchmark import verify_checkpoint_benchmark
from canonical_checkpoint_current_clone_schema import install_current_review_index
from canonical_checkpoint_deep_history_acceptance import (
    verify_deep_history,
    verify_materialized_limit_is_atomic,
)
from canonical_checkpoint_fault_rehearsal import verify_checkpoint_fault_and_races
from canonical_checkpoint_http_acceptance import verify_checkpoint_http_contract
from canonical_checkpoint_paged_clone_schema import install_paged_checkpoint_recovery
from canonical_checkpoint_paged_http_acceptance import (
    MAPPING as A91_MAPPING,
)
from canonical_checkpoint_paged_http_acceptance import (
    ORIGINAL as A91_ORIGINAL,
)
from canonical_checkpoint_paged_http_acceptance import (
    STAGED as A91_STAGED,
)
from canonical_checkpoint_paged_http_acceptance import (
    _decisions as a91_decisions,
)
from canonical_checkpoint_paged_http_acceptance import (
    verify_paged_review_http,
)
from canonical_checkpoint_probe_clone_schema import install_checkpoint_probe
from canonical_p4_performance import benchmark_https_quote_reads
from canonical_p4_quote_scale import verify_quote_list_scale
from canonical_review_candidate_rehearsal import verify_candidate_upgrade_downgrade
from canonical_review_migration_rehearsal import (
    verify_actual_alembic_upgrade_downgrade,
    verify_review_migration_rollback,
)
from canonical_review_publication_acceptance import verify_atomic_review_publication
from canonical_review_query_plans import verify_review_query_plans
from dotenv import dotenv_values
from fastapi import FastAPI
from fastapi.testclient import TestClient
from postgres_clone_connection import drop_legacy_test_clone as drop_test_clone
from pydantic import SecretStr
from pypdf import PdfReader
from quote_negative_acceptance import (
    verify_catalog_quote_race_http,
    verify_competing_revisions_http,
    verify_negative_quote_http,
)
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL
from supplier_canonical_live_acceptance import verify_reviewed_catalog_tenant_https
from tenant_browser_harness import run_tenant_browser_smoke
from tenant_cache_acceptance import verify_tenant_cache_http

from kpdesk.api.routes import get_groq_client, get_groq_settings
from kpdesk.api.tenant_company_lookup_routes import get_company_lookup_service
from kpdesk.api.tenant_session_routes import SESSION_COOKIE
from kpdesk.catalog.review_worker_admission import ReviewWorkerPublicationGate
from kpdesk.company.lookup import CompanyLookupService, LookupSettings
from kpdesk.config import AppSettings
from kpdesk.db.auth_sessions import AuthSessionRepository, hash_password
from kpdesk.db.live_service_engines import _restricted_engine
from kpdesk.extraction.groq import GroqSettings
from kpdesk.main import create_app

ROOT = Path(__file__).resolve().parents[2]


def _owner_engine(database: str, *, autocommit: bool = False) -> Engine:
    settings = dotenv_values(ROOT / ".env")
    password = settings.get("KP_DESK_DB_PASSWORD")
    assert isinstance(password, str) and len(password) >= 32
    return create_engine(URL.create(
        "postgresql+psycopg", username="kpdesk", password=password,
        host="127.0.0.1", port=int(settings.get("KP_DESK_DB_PORT") or 55432),
        database=database,
    ), isolation_level="AUTOCOMMIT" if autocommit else None,
       hide_parameters=True)


@pytest.mark.skipif(
    os.environ.get("KP_DESK_RUN_A45_LIVE_ROLE_BROWSER") != "1",
    reason="Explicit opt-in: disposable full-schema PostgreSQL clone plus HTTPS Chrome",
)
def test_https_real_auth_and_tenant_logins_with_manager_audit() -> None:
    if os.environ.get('KP_DESK_E2E_RELEASE_RUNTIME_ONLY') == '1':
        if os.environ.get('KP_DESK_E2E_RELEASE_RUNTIME') != '1':
            raise RuntimeError('Release-only mode requires explicit release-runtime opt-in')
        from canonical_release_runtime_acceptance import verify_release_runtime

        release_admin = _owner_engine('postgres', autocommit=True)
        try:
            verify_release_runtime(release_admin, _owner_engine)
        finally:
            release_admin.dispose()
        print('RELEASE_RUNTIME_ONLY: new full-migration clone and provider journey; historical A45 scenarios not rerun')
        return
    nonce = secrets.token_hex(6)
    name = f"kpdesk_accounts_http_{nonce}"
    assert re.fullmatch(r"kpdesk_accounts_http_[0-9a-f]{12}", name)
    email = f"a45-{nonce}@example.invalid"
    password = "Synthetic-Testing-Only-Correct-2026!"
    actor, second_actor, organization, outsider = (
        uuid.uuid4() for _ in range(4)
    )
    admin = _owner_engine("postgres", autocommit=True)
    created = False
    engines: list[Engine] = []
    try:
        live = _owner_engine("kpdesk")
        try:
            with live.connect() as connection:
                assert connection.scalar(
                    text("SELECT version_num FROM alembic_version")
                ) == "0031_catalog_taxonomy_v4"
                assert connection.execute(text("""
                    SELECT (SELECT count(*) FROM public.users),
                           (SELECT count(*) FROM public.quote_documents),
                           (SELECT count(*) FROM public.organizations)
                """)).one() == (0, 0, 0)
        finally:
            live.dispose()

        with admin.connect() as connection:
            connection.exec_driver_sql(
                f'CREATE DATABASE "{name}" TEMPLATE template0 OWNER kpdesk'
            )
            created = True
            connection.exec_driver_sql(
                f"COMMENT ON DATABASE \"{name}\" IS 'kpdesk:accounts-http:{nonce}'"
            )

        result = subprocess.run(
            ["docker", "compose", "--env-file", ".env", "ps", "-q", "db"],
            cwd=ROOT, capture_output=True, text=True, check=True, timeout=15,
        )
        container_id = result.stdout.strip()
        assert container_id and re.fullmatch("[a-f0-9]{12,64}", container_id)
        with subprocess.Popen(
            ["docker", "exec", container_id, "pg_dump", "-U", "kpdesk",
             "-d", "kpdesk", "-Fc", "--no-owner"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ) as backup:
            assert backup.stdout is not None
            restored = subprocess.run(
                ["docker", "exec", "-i", container_id, "pg_restore",
                 "-U", "kpdesk", "-d", name, "--exit-on-error", "--no-owner"],
                stdin=backup.stdout, capture_output=True,
                check=False, timeout=120,
            )
            backup.stdout.close()
            assert backup.wait(timeout=120) == 0
            assert restored.returncode == 0, restored.stderr.decode(
                "utf-8", "replace",
            )[-1000:]

        clone = _owner_engine(name)
        try:
            with clone.begin() as connection:
                existing_rev = connection.scalar(
                    text("SELECT version_num FROM alembic_version")
                )
                if existing_rev == "0029_manager_decisions_read":
                    with Operations.context(MigrationContext.configure(connection)):
                        importlib.import_module(
                            "migrations.versions.0030_tenant_llm_cache"
                        ).upgrade()
                    connection.execute(text("""
                        UPDATE alembic_version
                        SET version_num='0030_tenant_llm_cache'
                        WHERE version_num='0029_manager_decisions_read'
                    """))
                elif existing_rev == "0031_catalog_taxonomy_v4":
                    # The source is the current 0031 release. Downgrade the
                    # disposable EMPTY clone to 0030 because historical A45
                    # diagnostics explicitly exercise the pre-0031 behavior.
                    config = Config(str(ROOT / "backend" / "alembic.ini"))
                    config.attributes["connection"] = connection
                    command.downgrade(config, "0030_tenant_llm_cache")
                    assert connection.scalar(text(
                        "SELECT version_num FROM alembic_version"
                    )) == "0030_tenant_llm_cache"
                else:
                    raise RuntimeError("A45 clone source revision unexpected")
                connection.execute(text("""
                    INSERT INTO public.organizations(id,name,status)
                    VALUES (:org,'A45 isolated owner','active'),
                           (:outsider,'A45 inaccessible org','active')
                """), {"org": organization, "outsider": outsider})
                connection.execute(text("""
                    INSERT INTO public.users(
                        id,email,password_hash,status,email_verified_at
                    ) VALUES (:actor,:email,:hashed,'active',clock_timestamp())
                """), {
                    "actor": actor, "email": email,
                    "hashed": hash_password(password),
                })
                connection.execute(text("""
                    INSERT INTO public.users(
                        id,email,password_hash,status,email_verified_at
                    ) VALUES (:actor,:email,:hashed,'active',clock_timestamp())
                """), {
                    "actor": second_actor,
                    "email": f"a48-second-{nonce}@example.invalid",
                    "hashed": hash_password(password),
                })
                connection.execute(text("""
                    INSERT INTO public.organization_memberships(
                        organization_id,user_id,role,status
                    ) VALUES (:org,:actor,'owner','active'),
                             (:org,:second_actor,'editor','active')
                """), {
                    "org": organization, "actor": actor,
                    "second_actor": second_actor,
                })
        finally:
            clone.dispose()

        # New settings DDL is rehearsed only on this marked disposable clone.
        profile_owner = _owner_engine(name)
        try:
            from kpdesk.db.company_logo_schema import install_company_logo_on_clone
            from kpdesk.db.company_profile_schema import install_company_profile_on_clone
            from kpdesk.db.issuer_requisites_schema import install_issuer_requisites_on_clone

            with profile_owner.begin() as connection:
                from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone

                assert is_live_accounts_http_clone(connection)
                # Rehearse the actual new migration on the empty tagged clone,
                # then return to A45's historical baseline before its diagnostics.
                config = Config(str(ROOT / "backend" / "alembic.ini"))
                config.attributes.update({
                    "connection": connection, "kpdesk_review_schema_0033_approved": True,
                    "kpdesk_review_bridge_0034_approved": True,
                    "kpdesk_supplier_discovery_0035_approved": True,
                    "kpdesk_supplier_registry_0036_approved": True,
                    "kpdesk_company_profile_0037_approved": True,
                    "kpdesk_issuer_requisites_0038_approved": True,
                    "kpdesk_company_logo_0039_approved": True,
                    "kpdesk_training_feedback_0040_approved": True,
                })
                rehearsal = connection.begin_nested()
                try:
                    connection.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public"))
                    command.upgrade(config, "0039_company_logo")
                    if os.environ.get('KP_DESK_E2E_FEEDBACK_STORAGE') == '1':
                        command.upgrade(config, '0040_training_feedback')
                        assert connection.scalar(text("SELECT relforcerowsecurity FROM pg_class WHERE oid='public.training_feedback_records'::regclass"))
                        command.downgrade(config, '0039_company_logo')
                        assert connection.scalar(text("SELECT to_regclass('public.training_feedback_records')")) is None
                    assert connection.scalar(text("SELECT relforcerowsecurity FROM pg_class WHERE oid='public.organization_proposal_profiles'::regclass"))
                    command.downgrade(config, "0036_supplier_registry")
                    assert connection.scalar(text("SELECT to_regclass('public.organization_proposal_profiles')")) is None
                finally:
                    # 0032 correctly forbids downgrade. Roll back the clone-only
                    # DDL savepoint instead of weakening its immutable audit gate.
                    rehearsal.rollback()
                assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0030_tenant_llm_cache"
                install_company_profile_on_clone(connection)
                install_issuer_requisites_on_clone(connection)
                install_company_logo_on_clone(connection)
                if os.environ.get('KP_DESK_E2E_FEEDBACK_STORAGE') == '1':
                    from kpdesk.db.training_feedback_schema import (
                        install_training_feedback_on_clone,
                    )

                    install_training_feedback_on_clone(connection)
        finally:
            profile_owner.dispose()

        credentials = json.loads(
            (ROOT / "credentials" / "kpdesk-runtime-db.json").read_text(
                encoding="utf-8",
            ),
        )
        settings = dotenv_values(ROOT / ".env")
        port = int(settings.get("KP_DESK_DB_PORT") or 55432)
        auth = _restricted_engine(
            "kpdesk_auth_service", "kpdesk_auth_runtime",
            credentials["kpdesk_auth_service"], port, database=name,
        )
        tenant = _restricted_engine(
            "kpdesk_tenant_service", "kpdesk_tenant_runtime",
            credentials["kpdesk_tenant_service"], port, database=name,
        )
        engines.extend((auth, tenant))
        with auth.begin() as auth_conn, tenant.begin() as tenant_conn:
            assert tenant_conn.scalar(text("SELECT public.kpdesk_company_profile_read(:org)"), {"org": organization}) is None
            assert tenant_conn.scalar(text("SELECT has_table_privilege(current_user,'public.organization_proposal_profiles','SELECT')")) is False
            assert tenant_conn.scalar(text("SELECT has_table_privilege(current_user,'public.organization_proposal_profiles','UPDATE')")) is False
            assert auth_conn.scalar(text("SELECT current_user")) == "kpdesk_auth_runtime"
            assert tenant_conn.scalar(text("SELECT current_user")) == (
                "kpdesk_tenant_runtime"
            )
            assert auth_conn.scalar(text("SELECT pg_backend_pid()")) != (
                tenant_conn.scalar(text("SELECT pg_backend_pid()"))
            )
            assert auth_conn.scalar(text("""
                SELECT has_table_privilege(current_user,'public.catalog_items','SELECT')
            """)) is False
            assert tenant_conn.scalar(text("""
                SELECT has_function_privilege(
                  current_user,'public.kpdesk_auth_credential(text,text)','EXECUTE'
                )
            """)) is False

        def timed_out_groq(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if body['tools'][0]['function']['name'] == "kpdesk_proposal_fact_review_v2":
                verdict = {name: {'status': 'supported', 'issues': []}
                           for name in ('headline', 'opening', 'solution', 'closing')}
                return httpx.Response(200, json={"model": "nvidia/nemotron-3-ultra-550b-a55b:free", 'choices': [{'finish_reason': 'stop',
                                       'message': {'content': json.dumps(verdict)}}]})
            if body['tools'][0]['function']['name'] == "kpdesk_commercial_proposal_v2":
                return httpx.Response(200, json={"model": "nvidia/nemotron-3-ultra-550b-a55b:free", "choices": [{
                    "finish_reason": "stop", "message": {"content": json.dumps({
                        "headline": "Решение по комплектации офиса",
                        "opening": "Уважаемые коллеги! Предлагаем решение по вашей заявке.",
                        "solution": "Предложение включает подобранные товары из прайса. Состав и стоимость приведены в таблице.",
                        "closing": "Готовы обсудить заказ и согласовать условия поставки.",
                    }, ensure_ascii=False)},
                }]})
            raise httpx.ReadTimeout("A48 synthetic provider timeout")

        async def synthetic_groq_client() -> AsyncIterator[httpx.AsyncClient]:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(timed_out_groq),
            ) as client:
                yield client

        # Real local DaData credentials must never be used by this clone test.
        offline_dadata_calls: list[str] = []

        def offline_dadata(request: httpx.Request) -> httpx.Response:
            offline_dadata_calls.append(str(request.url))
            assert request.headers['Authorization'] == 'Token a45-offline-dadata'
            if json.loads(request.content)['query'] == '7736207543':
                return httpx.Response(200, json={'suggestions': [{'data': {
                    'name': {
                        'full_with_opf': 'Общество с ограниченной ответственностью «Учебный заказчик»',
                        'short_with_opf': 'Учебный заказчик',
                    },
                    'inn': '7736207543', 'kpp': '773601001', 'ogrn': '1027700229193',
                    'address': {'unrestricted_value': 'Учебный адрес'},
                    'state': {'status': 'ACTIVE'}, 'branch_type': 'MAIN', 'type': 'LEGAL',
                }}]})
            return httpx.Response(200, json={'suggestions': []})

        offline_company_lookup = CompanyLookupService(
            LookupSettings(api_key=SecretStr('a45-offline-dadata')),
            transport=httpx.MockTransport(offline_dadata),
        )

        def factory(origin: str) -> FastAPI:
            with patch(
                "kpdesk.db.live_service_engines.live_service_engines",
                return_value=(auth, tenant),
            ):
                app = create_app(
                    testing=True, tenant_sessions=True,
                    settings=AppSettings(
                        allowed_tenant_origins=(origin,), enable_synthetic_ml=True,
                    ),
                )
                app.dependency_overrides[get_groq_settings] = lambda: GroqSettings(test_project_approved=True, 
                    api_key="a48-synthetic-no-network",
                )
                app.dependency_overrides[get_groq_client] = synthetic_groq_client
                app.dependency_overrides[get_company_lookup_service] = lambda: offline_company_lookup
                return app

        log = run_tenant_browser_smoke(
            email=email, password=password, tenant_app_factory=factory,
            simulate_groq_timeout=True,
        )
        assert "PostgreSQL A44 manager decision attribution: PASS" in log
        assert "HTTPS new quote resets previous editor and SKU selection: PASS" in log
        assert "HTTPS logout → repeat login → saved quote reopen: PASS" in log
        assert "A48 Groq timeout → safe manual-only rows: PASS" in log
        assert "A59 two-row human review → distinct SKU → 2209.80 RUB → PDF → reopen: PASS" in log
        assert "A60 partial quote PDF blocked → repair one line → original SKU retained → PDF: PASS" in log
        assert "A61 reopen restores typed request but requires fresh per-line review and SKU: PASS" in log
        assert "AI proposal text → manager approval → immutable client PDF: PASS" in log

        with auth.begin() as connection:
            owner_session = AuthSessionRepository(connection).login(
                email, password, peer="127.0.0.1",
            )
            second_session = AuthSessionRepository(connection).login(
                f"a48-second-{nonce}@example.invalid",
                password, peer="127.0.0.2",
            )
        with TestClient(factory("https://testserver"), base_url="https://testserver") as client:
            own_quotes = f"/api/session/organizations/{organization}/quotes"
            assert client.get(own_quotes).status_code == 401
            client.cookies.set(SESSION_COOKIE, owner_session.session_token)
            resumed = client.get('/api/session/resume')
            assert resumed.status_code == 200, resumed.text
            assert resumed.headers['cache-control'] == 'no-store'
            assert resumed.json() == {
                'user_id': str(actor), 'csrf_token': owner_session.csrf_token,
            }
            assert client.post(
                f'/api/session/organizations/{organization}/catalog/preflight',
                headers={'Origin': 'https://testserver',
                         'X-CSRF-Token': resumed.json()['csrf_token']},
                files={'file': ('invalid.csv', b'not a catalog', 'text/csv')},
            ).status_code == 200
            listing = client.get(own_quotes)
            assert listing.status_code == 200, listing.text
            items = listing.json()["items"]
            assert len(items) == 3
            original_quotes = [item for item in items if item["total_rub"] == "450.00"]
            multiline_quotes = [
                item for item in items if item["total_rub"] == "2209.80"
                and item["revision"] == 1
            ]
            repaired_quotes = [
                item for item in items if item["total_rub"] == "2209.80"
                and item["revision"] == 3  # Revision 2 is preserved; AI offer approval appends 3.
            ]
            assert len(original_quotes) == len(multiline_quotes) == len(repaired_quotes) == 1
            saved_id = original_quotes[0]["quote_id"]
            multiline_id = multiline_quotes[0]["quote_id"]
            repaired_id = repaired_quotes[0]["quote_id"]
            multiline_response = client.get(f"{own_quotes}/{multiline_id}")
            assert multiline_response.status_code == 200, multiline_response.text
            multiline = multiline_response.json()
            assert multiline["ready_for_draft"] is True
            assert multiline["approved"] is False
            assert multiline["revision"] == 1
            assert multiline["total_rub"] == "2209.80"
            assert [line["quantity"] for line in multiline["source_request"]["lines"]] == [2, 3]
            assert [line["paper_gsm"] for line in multiline["source_request"]["lines"]] == [80, 80]
            assert [line["paper_format"] for line in multiline["source_request"]["lines"]] == ["A4", "A4"]
            assert [(row["sku"], row["sale_quantity"], row["unit_price_rub"],
                     row["line_total_rub"], row["status"]) for row in multiline["lines"]] == [
                ("E2E-PAPER-A", 2, "429.90", "859.80", "manager_selected"),
                ("E2E-PAPER-B", 3, "450.00", "1350.00", "manager_selected"),
            ]
            multiline_pdf = client.get(f"{own_quotes}/{multiline_id}/pdf")
            assert multiline_pdf.status_code == 200
            reader = PdfReader(BytesIO(multiline_pdf.content))
            pdf_text = "\n".join(page.extract_text() for page in reader.pages)
            assert all(value in pdf_text for value in (
                "E2E-PAPER-A", "E2E-PAPER-B", "429.90", "450.00",
                "859.80", "1350.00", "2209.80", multiline_id,
                "Черновик", "Не утверждено",
            ))

            # Preview/lookup are private reads: no new quote revision or approval.
            # All SQL below is scoped to the already marked disposable clone.
            from test_recipient_requisites import document_with_recipient

            clone_readback = _owner_engine(name)
            try:
                with clone_readback.connect() as connection:
                    from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone

                    assert is_live_accounts_http_clone(connection)
                    preview_counts_before = connection.execute(text('''
                        SELECT
                          (SELECT count(*) FROM public.quote_revisions),
                          (SELECT count(*) FROM public.organization_proposal_profiles),
                          (SELECT current_revision FROM public.quote_documents
                           WHERE organization_id=:org AND id=:quote)
                    '''), {'org': organization, 'quote': multiline_id}).one()
                preview_url = f'{own_quotes}/{multiline_id}/document-preview'
                lookup_url = f'/api/session/organizations/{organization}/company-lookup'
                preview_body = {
                    'expected_revision': multiline['revision'],
                    'source_snapshot_sha256': multiline['snapshot_sha256'],
                    'document': document_with_recipient().model_dump(mode='json'),
                }
                private_headers = {'Origin': 'https://testserver',
                                   'X-CSRF-Token': owner_session.csrf_token}
                calls_before = len(offline_dadata_calls)
                with TestClient(factory('https://testserver'), base_url='https://testserver') as anonymous:
                    for url, body in ((preview_url, preview_body), (lookup_url, {'inn': '7707083893'})):
                        assert anonymous.post(url, headers={'Origin': 'https://testserver'}, json=body).status_code == 401
                for url, body in ((preview_url, preview_body), (lookup_url, {'inn': '7707083893'})):
                    assert client.post(url, headers={'Origin': 'https://testserver'}, json=body).status_code == 401
                    assert client.post(url.replace(str(organization), str(outsider)),
                                       headers=private_headers, json=body).status_code == 403
                assert len(offline_dadata_calls) == calls_before
                preview_pdf = client.post(preview_url, headers=private_headers, json=preview_body)
                assert preview_pdf.status_code == 200, preview_pdf.text
                assert preview_pdf.headers['cache-control'] == 'no-store'
                assert preview_pdf.headers['content-type'] == 'application/pdf'
                preview_pages = PdfReader(BytesIO(preview_pdf.content)).pages
                assert all('ПРЕДПРОСМОТР' in page.extract_text() and 'НЕ УТВЕРЖДЕНО' in page.extract_text()
                           for page in preview_pages)
                preview_text = '\n'.join(page.extract_text() for page in preview_pages)
                assert 'Реквизиты заказчика' in preview_text and '7707083893' in preview_text
                stale_preview = client.post(preview_url, headers=private_headers,
                                            json={**preview_body, 'source_snapshot_sha256': 'b' * 64})
                assert stale_preview.status_code == 409
                lookup_first = client.post(lookup_url, headers=private_headers, json={'inn': '7707083893'})
                assert lookup_first.status_code == 200, lookup_first.text
                assert lookup_first.headers['cache-control'] == 'no-store'
                assert lookup_first.json()['party'] is None and lookup_first.json()['cached'] is False
                lookup_second = client.post(lookup_url, headers=private_headers, json={'inn': '7707083893'})
                assert lookup_second.status_code == 200 and lookup_second.json()['cached'] is True
                assert lookup_second.json()['fetched_at'] == lookup_first.json()['fetched_at']
                assert len(offline_dadata_calls) == calls_before + 1
                assert client.get(f'{own_quotes}/{multiline_id}').json() == multiline
                with clone_readback.connect() as connection:
                    preview_counts_after = connection.execute(text('''
                        SELECT
                          (SELECT count(*) FROM public.quote_revisions),
                          (SELECT count(*) FROM public.organization_proposal_profiles),
                          (SELECT current_revision FROM public.quote_documents
                           WHERE organization_id=:org AND id=:quote)
                    '''), {'org': organization, 'quote': multiline_id}).one()
                assert preview_counts_after == preview_counts_before
            finally:
                clone_readback.dispose()
            multiline_decisions = client.get(
                f"{own_quotes}/{multiline_id}/manager-decisions/1"
            )
            assert multiline_decisions.status_code == 200
            assert [
                (row["line_number"], row["sku"], row["approved_by_user_id"])
                for row in multiline_decisions.json()["items"]
            ] == [
                (1, "E2E-PAPER-A", str(actor)),
                (2, "E2E-PAPER-B", str(actor)),
            ]
            assert client.get(
                f"/api/session/organizations/{outsider}/quotes/{multiline_id}/pdf"
            ).status_code == 403
            incomplete = client.get(f"{own_quotes}/{repaired_id}", params={"revision": 1})
            assert incomplete.status_code == 200
            before = incomplete.json()
            assert before["revision"] == 1
            assert before["ready_for_draft"] is False
            assert before["total_rub"] is None
            assert before["approved"] is False
            assert len(before["lines"]) == 2
            assert before["lines"][0]["status"] == "manager_selected"
            assert before["lines"][0]["sku"] == "E2E-PAPER-A"
            assert before["lines"][0]["line_total_rub"] == "859.80"
            assert before["lines"][1]["status"] in ("no_match", "needs_review")
            assert before["lines"][1]["sku"] is None
            assert before["source_request"]["lines"][1]["paper_gsm"] == 120
            assert client.get(
                f"{own_quotes}/{repaired_id}/pdf", params={"revision": 1}
            ).status_code == 409
            old_decisions = client.get(f"{own_quotes}/{repaired_id}/manager-decisions/1")
            assert old_decisions.status_code == 200
            assert [(row["line_number"], row["sku"]) for row in
                    old_decisions.json()["items"]] == [(1, "E2E-PAPER-A")]

            completed = client.get(f"{own_quotes}/{repaired_id}", params={"revision": 2})
            assert completed.status_code == 200
            after = completed.json()
            assert after["revision"] == 2
            assert after["ready_for_draft"] is True
            assert after["total_rub"] == "2209.80"
            assert after["approved"] is False
            assert after["source_request"]["lines"][1]["paper_gsm"] == 80
            assert after["source_request"]["lines"][0] == before["source_request"]["lines"][0]
            assert before["snapshot_sha256"] != after["snapshot_sha256"]
            assert [(row["sku"], row["line_total_rub"], row["status"]) for row in
                    after["lines"]] == [
                ("E2E-PAPER-A", "859.80", "manager_selected"),
                ("E2E-PAPER-B", "1350.00", "manager_selected"),
            ]
            assert before["lines"][0] == after["lines"][0]
            updated_decisions = client.get(
                f"{own_quotes}/{repaired_id}/manager-decisions/2"
            )
            assert updated_decisions.status_code == 200
            assert [
                (row["line_number"], row["sku"], row["approved_by_user_id"])
                for row in updated_decisions.json()["items"]
            ] == [
                (1, "E2E-PAPER-A", str(actor)),
                (2, "E2E-PAPER-B", str(actor)),
            ]
            finished_pdf = client.get(f"{own_quotes}/{repaired_id}/pdf", params={"revision": 2})
            assert finished_pdf.status_code == 200
            content = "\n".join(
                page.extract_text() for page in PdfReader(BytesIO(finished_pdf.content)).pages
            )
            assert all(part in content for part in (
                repaired_id, "E2E-PAPER-A", "E2E-PAPER-B", "859.80",
                "1350.00", "2209.80", "Черновик", "Не утверждено",
            ))
            approved_document = client.get(f"{own_quotes}/{repaired_id}").json()
            assert approved_document["revision"] == 3 and approved_document["approved"]
            assert approved_document["lines"] == after["lines"]
            assert approved_document["approval"]["source_snapshot_sha256"] == after["snapshot_sha256"]
            offered_pdf = client.get(f"{own_quotes}/{repaired_id}/pdf")
            assert offered_pdf.status_code == 200
            offered_text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(offered_pdf.content)).pages)
            assert "Уважаемые коллеги!" in offered_text and "Закупка" not in offered_text
            assert "Не утверждено" not in offered_text
            assert client.get(
                f"/api/session/organizations/{outsider}/quotes/{repaired_id}/pdf"
            ).status_code == 403
            assert client.get(
                f"/api/session/organizations/{outsider}/quotes/{repaired_id}"
            ).status_code == 403
            decision = f"{own_quotes}/{saved_id}/manager-decisions/1"
            audit = client.get(decision)
            assert audit.status_code == 200, audit.text
            assert audit.json()["items"][0]["approved_by_user_id"] == str(actor)
            assert audit.json()["items"][0]["sku"] == "E2E-PAPER-B"
            assert audit.headers["cache-control"] == "no-store"
            assert client.get(
                decision.replace(str(organization), str(outsider))
            ).status_code == 403
            assert client.get(
                decision.replace(saved_id, str(uuid.uuid4()))
            ).status_code == 404
            negative_engine = _owner_engine(name)
            try:
                with TestClient(
                    factory("https://testserver"), base_url="https://testserver",
                ) as other_client:
                    other_client.cookies.set(
                        SESSION_COOKIE, second_session.session_token,
                    )
                    original_catalog = client.get(
                        f"{own_quotes}/{saved_id}"
                    ).json()["catalog_version_id"]
                    next_catalog = verify_negative_quote_http(
                        client, other_client, negative_engine,
                        organization, outsider, saved_id,
                        owner_session.csrf_token, second_session.csrf_token,
                    )
                    race_winner = verify_competing_revisions_http(
                        client, other_client, negative_engine,
                        organization, saved_id, next_catalog,
                        owner_session.csrf_token, second_session.csrf_token,
                        original_catalog,
                    )
                    race_catalog_outcome, _ = verify_catalog_quote_race_http(
                        client, other_client, negative_engine, organization,
                        saved_id, next_catalog, owner_session.csrf_token,
                        second_session.csrf_token,
                    )
                verify_tenant_cache_http(
                    factory("https://testserver"), negative_engine, tenant,
                    organization, outsider, owner_session, second_session,
                )
            finally:
                negative_engine.dispose()

        clone = _owner_engine(name)
        try:
            with clone.connect() as connection:
                record = connection.execute(text("""
                    SELECT q.organization_id, d.quote_id, d.revision,
                           d.line_number, d.sku, d.approved_by_user_id
                    FROM public.tenant_quote_manager_decisions d
                    JOIN public.quote_documents q
                      ON q.organization_id=d.organization_id AND q.id=d.quote_id
                """)).all()
                expected = {
                    (organization, uuid.UUID(saved_id), 1, 1, "E2E-PAPER-B", actor),
                    (organization, uuid.UUID(saved_id), 2, 1, "E2E-PAPER-B", actor),
                    (organization, uuid.UUID(saved_id), 3, 1, "E2E-PAPER-B",
                     actor if race_winner == "owner" else second_actor),
                    (organization, uuid.UUID(multiline_id), 1, 1, "E2E-PAPER-A", actor),
                    (organization, uuid.UUID(multiline_id), 1, 2, "E2E-PAPER-B", actor),
                    (organization, uuid.UUID(repaired_id), 1, 1, "E2E-PAPER-A", actor),
                    (organization, uuid.UUID(repaired_id), 2, 1, "E2E-PAPER-A", actor),
                    (organization, uuid.UUID(repaired_id), 2, 2, "E2E-PAPER-B", actor),
                    (organization, uuid.UUID(repaired_id), 3, 1, "E2E-PAPER-A", actor),
                    (organization, uuid.UUID(repaired_id), 3, 2, "E2E-PAPER-B", actor),
                    (organization, uuid.UUID(saved_id),
                     5 if race_catalog_outcome == "quote_first" else 4,
                     1, "E2E-PAPER-B", second_actor),
                }
                if race_catalog_outcome == "quote_first":
                    expected.add((
                        organization, uuid.UUID(saved_id), 4, 1, "E2E-PAPER-B",
                        second_actor,
                    ))
                assert len(record) == len(expected)
                assert set(record) == expected
                assert connection.scalar(text("""
                    SELECT count(*) FROM public.quote_revisions
                    WHERE organization_id=:outsider
                """), {"outsider": outsider}) == 0
                assert connection.scalar(text("""
                    SELECT count(*) FROM public.quote_documents
                    WHERE organization_id=:org
                """), {"org": organization}) == 3
        finally:
            clone.dispose()

        # A69: independent real-role HTTP export -> preflight -> publish -> current.
        # Runs against the same disposable, provenance-tagged clone only.
        published_id = verify_reviewed_catalog_tenant_https(
            factory, organization, outsider, owner_session, second_session,
        )
        assert isinstance(published_id, uuid.UUID)
        # Read-only post-publication inspection: excluded source headings do not
        # silently become catalog items or leak into a different organization.
        verification_engine = _owner_engine(name)
        try:
            with verification_engine.connect() as connection:
                products = connection.execute(text("""
                    SELECT organization_id, sku, name, price_rub
                    FROM public.catalog_items
                    WHERE catalog_version_id=:version
                """), {"version": published_id}).all()
                assert len(products) == 1
                product = products[0]
                assert (
                    product.organization_id, product.sku, product.name,
                    str(product.price_rub),
                ) == (organization, "KEY-100", "Office keyboard", "1500.90")
                assert connection.scalar(text("""
                    SELECT count(*) FROM public.catalog_items
                    WHERE organization_id=:outsider AND catalog_version_id=:version
                """), {"outsider": outsider, "version": published_id}) == 0
        finally:
            verification_engine.dispose()

        # Keep historical quote/currency assertions on their original catalog;
        # the browser batch test publishes a NEW version and therefore runs last.
        batch_log = run_tenant_browser_smoke(
            email=email, password=password, tenant_app_factory=factory,
            review_supplier_batch=True,
        )
        assert "A71 HTTPS React batch" in batch_log
        # The last browser run must publish exactly the two explicitly selected
        # vendor SKUs; the section heading must remain excluded from storage.
        batch_check = _owner_engine(name)
        try:
            with batch_check.connect() as connection:
                current = connection.execute(text("""
                    SELECT a.catalog_version_id, v.source_sha256, v.rows_count
                    FROM public.tenant_catalog_active a
                    JOIN public.catalog_versions v
                      ON v.organization_id=a.organization_id
                     AND v.id=a.catalog_version_id
                    WHERE a.organization_id=:org
                """), {"org": organization}).one()
                assert current.catalog_version_id != published_id
                assert current.rows_count == 2
                products = connection.execute(text("""
                    SELECT sku, name, price_rub FROM public.catalog_items
                    WHERE organization_id=:org AND catalog_version_id=:version
                    ORDER BY sku
                """), {
                    "org": organization, "version": current.catalog_version_id,
                }).all()
                assert [
                    (item.sku, item.name, str(item.price_rub)) for item in products
                ] == [
                    ("KB-A", "Office keyboard wired 104 keys", "1990.00"),
                    ("KB-B", "Office keyboard wired 104 keys second", "2100.00"),
                ]
                assert connection.scalar(text("""
                    SELECT count(*) FROM public.catalog_items
                    WHERE organization_id=:outsider
                      AND catalog_version_id=:version
                """), {
                    "outsider": outsider, "version": current.catalog_version_id,
                }) == 0
        finally:
            batch_check.dispose()

        # A72 prototype is installed only on this disposable marked clone,
        # after the real HTTPS release checks. Never update Alembic or kpdesk.
        checkpoint_owner = _owner_engine(name)
        try:
            verify_review_migration_rollback(checkpoint_owner)
            verify_actual_alembic_upgrade_downgrade(
                checkpoint_owner, str(organization), str(actor),
            )
            verify_candidate_upgrade_downgrade(checkpoint_owner)
            verify_checkpoint_clone_contract(
                checkpoint_owner, tenant, organization, outsider,
                owner_session, second_session, actor,
            )
            with checkpoint_owner.begin() as connection:
                install_current_review_index(connection)
            verify_checkpoint_http_contract(
                factory, organization, outsider,
                owner_session, second_session, actor,
            )
            with checkpoint_owner.begin() as connection:
                install_paged_checkpoint_recovery(connection)
                install_checkpoint_probe(connection)
            verify_paged_review_http(
                factory, organization, outsider,
                owner_session, second_session,
            )
            verify_checkpoint_benchmark(tenant, organization, owner_session)
            verify_review_query_plans(checkpoint_owner, organization)
            p4_report = benchmark_https_quote_reads(
                owner=checkpoint_owner, auth=auth, tenant=tenant,
                organization=organization, session=owner_session,
                quote_id=multiline_id, factory=factory,
                output_dir=ROOT / 'artifacts',
            )
            scenarios = p4_report['scenarios']
            assert isinstance(scenarios, list) and len(scenarios) == 3
            verify_materialized_limit_is_atomic(
                tenant, checkpoint_owner, organization, owner_session,
            )
            verify_deep_history(
                tenant, checkpoint_owner, organization, outsider,
                owner_session, second_session,
            )
            verify_checkpoint_fault_and_races(
                tenant, checkpoint_owner, organization, outsider, owner_session,
            )
            active_review_signer = verify_atomic_review_publication(
                tenant, checkpoint_owner, organization, outsider, actor, owner_session,
                factory,
            )
            # A91: real React component + restricted-role HTTPS SQL session.
            def signed_factory(origin: str) -> FastAPI:
                app = factory(origin)
                app.state.a85_review_signing_credential = active_review_signer
                app.state.a94_review_worker_gate = ReviewWorkerPublicationGate(
                    active_review_signer,
                )
                return app

            fixture_decisions = [{
                **({
                    "brand": None, "paper_format": None, "paper_gsm": None,
                    "pen_ink_color": None, "pen_ink_type": None,
                    "price_unit_review_evidence": None, "precision_review": None,
                } if item["action"] == "include" else {}),
                **item,
            } for item in a91_decisions()]
            # Distinct existing editor account: no bypass or reset of the
            # production PostgreSQL account/peer login attempt limits.
            a91_log = run_tenant_browser_smoke(
                email=f"a48-second-{nonce}@example.invalid",
                password=password, tenant_app_factory=signed_factory,
                large_review_fixture={
                    "organization_id": str(organization),
                    "supplier_id": "supplier-a91-react",
                    "source": A91_ORIGINAL.decode("utf-8"),
                    "source_sha256": A91_STAGED.sha256,
                    "mapping": A91_MAPPING,
                    "decisions": fixture_decisions,
                },
            )
            assert "A91 React → real HTTPS 3 review pages" in a91_log
            with checkpoint_owner.connect() as connection:
                a91_active = connection.execute(text("""
                    SELECT a.catalog_version_id,v.rows_count,v.source_sha256
                    FROM public.tenant_catalog_active a
                    JOIN public.catalog_versions v
                      ON v.organization_id=a.organization_id
                     AND v.id=a.catalog_version_id
                    WHERE a.organization_id=:org
                """), {"org": organization}).one()
                assert a91_active.rows_count == 120
                assert connection.scalar(text("""
                    SELECT count(*) FROM public.a79_catalog_review_provenance
                    WHERE organization_id=:org AND catalog_version_id=:version
                      AND supplier_id='supplier-a91-react'
                """), {
                    "org": organization, "version": a91_active.catalog_version_id,
                }) == 1
                assert connection.scalar(text("""
                    SELECT count(*) FROM public.catalog_items
                    WHERE organization_id=:org AND catalog_version_id=:version
                      AND sku LIKE 'PG-%'
                """), {
                    "org": organization, "version": a91_active.catalog_version_id,
                }) == 120
            with checkpoint_owner.connect() as connection:
                indexes = connection.execute(text("""
                    SELECT indexname,indexdef FROM pg_indexes
                    WHERE schemaname='public' AND tablename=:table
                """), {'table': 'a72_canonical_checkpoints'}).all()
                assert any(
                    row.indexname == 'a72_scope_latest_idx'
                    and 'created_at DESC' in row.indexdef
                    and 'price_basis' in row.indexdef
                    for row in indexes
                )
                # These pre-existing constraint indexes should not be recreated.
                assert any('idempotency_key' in row.indexdef for row in indexes)
                decision_indexes: list[str] = []
                for index_row in connection.execute(text("""
                    SELECT indexdef FROM pg_indexes
                    WHERE schemaname='public'
                      AND tablename='a72_canonical_row_decisions'
                """)).all():
                    decision_indexes.append(str(index_row[0]))
                assert any('checkpoint_id, source_line' in item for item in decision_indexes)
                scope = connection.execute(text("""
                    SELECT source_sha256,mapping_sha256,taxonomy_version,price_basis
                    FROM public.a72_canonical_checkpoints
                    WHERE organization_id=:org AND supplier_id='supplier-a75'
                    LIMIT 1
                """), {'org': organization}).one()
                plan = connection.scalar(text("""
                    EXPLAIN (FORMAT JSON, ANALYZE, BUFFERS)
                    SELECT checkpoint_id FROM public.a72_canonical_checkpoints
                    WHERE organization_id=:org AND supplier_id=:supplier
                      AND source_sha256=:source AND mapping_sha256=:mapping
                      AND taxonomy_version=:taxonomy AND price_basis=:basis
                    ORDER BY created_at DESC,checkpoint_id DESC LIMIT 1
                """), {
                    'org': organization, 'supplier': 'supplier-a75',
                    'source': scope.source_sha256, 'mapping': scope.mapping_sha256,
                    'taxonomy': scope.taxonomy_version, 'basis': scope.price_basis,
                })
                assert plan and plan[0]['Plan']['Actual Rows'] == 1
                benchmark_scope = connection.execute(text("""
                    SELECT source_sha256,mapping_sha256,taxonomy_version,price_basis
                    FROM public.a72_canonical_checkpoints
                    WHERE organization_id=:org AND supplier_id='supplier-a76-benchmark'
                    LIMIT 1
                """), {'org': organization}).one()
                benchmark_parameters = {
                    'org': organization, 'supplier': 'supplier-a76-benchmark',
                    'source': benchmark_scope.source_sha256,
                    'mapping': benchmark_scope.mapping_sha256,
                    'taxonomy': benchmark_scope.taxonomy_version,
                    'basis': benchmark_scope.price_basis,
                }
                assert connection.scalar(text("""
                    SELECT count(*) FROM public.a72_canonical_checkpoints
                    WHERE organization_id=:org AND supplier_id=:supplier
                """), benchmark_parameters) == 100
                assert connection.scalar(text("""
                    SELECT count(*) FROM public.a72_canonical_row_decisions d
                    JOIN public.a72_canonical_checkpoints c
                      ON c.checkpoint_id=d.checkpoint_id
                    WHERE c.organization_id=:org AND c.supplier_id=:supplier
                """), benchmark_parameters) == 5000
                benchmark_plan = connection.scalar(text("""
                    EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON)
                    SELECT checkpoint_id FROM public.a72_canonical_checkpoints
                    WHERE organization_id=:org AND supplier_id=:supplier
                      AND source_sha256=:source AND mapping_sha256=:mapping
                      AND taxonomy_version=:taxonomy AND price_basis=:basis
                    ORDER BY created_at DESC,checkpoint_id DESC LIMIT 1
                """), benchmark_parameters)
                assert benchmark_plan[0]['Plan']['Actual Rows'] == 1
                print('A76_BENCHMARK_HEAD_PLAN', {
                    'node': benchmark_plan[0]['Plan']['Node Type'],
                    'child': benchmark_plan[0]['Plan'].get('Plans', [{}])[0]['Node Type'],
                    'execution_ms': round(benchmark_plan[0]['Execution Time'], 3),
                })
                a77_page_plan = connection.scalar(text("""
                    EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON)
                    SELECT source_line,decision FROM public.a77_current_row
                    WHERE organization_id=:org AND supplier_id=:supplier
                      AND source_sha256=:source AND mapping_sha256=:mapping
                      AND taxonomy_version=:taxonomy AND price_basis=:basis
                    ORDER BY source_line LIMIT 50 OFFSET 4950
                """), benchmark_parameters)
                assert a77_page_plan[0]['Plan']['Actual Rows'] == 50
                print('A77_CURRENT_STATE_PAGE_PLAN', {
                    'node': a77_page_plan[0]['Plan']['Node Type'],
                    'child': a77_page_plan[0]['Plan'].get('Plans', [{}])[0]['Node Type'],
                    'execution_ms': round(a77_page_plan[0]['Execution Time'], 3),
                })
            # Portfolio: use the *same downloadable synthetic CSV* as README
            # through actual React, real HTTPS, restricted PostgreSQL roles,
            # server-side saved quote revision and the real PDF response.
            # Do not introduce a fixture-only imitation of the UI.
            portfolio_log = run_tenant_browser_smoke(
                email=f"a48-second-{nonce}@example.invalid",
                password=password, tenant_app_factory=factory,
                portfolio_sample=True,
            )
            assert (
                "PORTFOLIO HTTPS CSV → 120gsm abstain → 80gsm ML "
                "→ 798 RUB quote → PDF: PASS"
            ) in portfolio_log
            with checkpoint_owner.connect() as connection:
                portfolio_catalog = connection.execute(text("""
                    SELECT a.catalog_version_id, v.rows_count
                    FROM public.tenant_catalog_active a
                    JOIN public.catalog_versions v
                      ON v.organization_id=a.organization_id
                     AND v.id=a.catalog_version_id
                    WHERE a.organization_id=:org
                """), {"org": organization}).one()
                assert portfolio_catalog.rows_count == 5
                catalog_prices = connection.execute(text("""
                    SELECT sku,price_rub FROM public.catalog_items
                    WHERE organization_id=:org AND catalog_version_id=:version
                    ORDER BY sku
                """), {
                    "org": organization,
                    "version": portfolio_catalog.catalog_version_id,
                }).all()
                assert len(catalog_prices) == 5
                assert sum(row.sku == "PAPER-A4-80" and row.price_rub == 399 for row in catalog_prices) == 1
                portfolio_quotes = connection.execute(text("""
                    SELECT d.id,d.current_revision,r.total_rub,r.is_complete
                    FROM public.quote_documents d
                    JOIN public.quote_revisions r
                      ON r.organization_id=d.organization_id
                     AND r.quote_id=d.id AND r.number=d.current_revision
                    WHERE d.organization_id=:org AND r.total_rub=798
                """), {"org": organization}).all()
                assert len(portfolio_quotes) == 1
                assert portfolio_quotes[0].current_revision == 1
                assert portfolio_quotes[0].is_complete is True
                assert portfolio_quotes[0].id is not None
            # Run quote-list scale last: historical quote revisions cannot be
            # deleted, and the whole marked clone is dropped in the outer finally.
            # Atomic publication intentionally revoked owner_session above.
            # The independently authorized editor session remains live.
            scale = verify_quote_list_scale(
                owner=checkpoint_owner, tenant=tenant,
                organization=organization, outsider=outsider,
                template_id=multiline_id, session=second_session, factory=factory,
                output_dir=ROOT / 'artifacts',
            )
            scale_scenarios = scale['scenarios']
            assert isinstance(scale_scenarios, list)
            assert [item['stored_quotes'] for item in scale_scenarios] == [100, 1000]
            # A96 deliberately follows quote-list scale so its extra document
            # cannot perturb the historical 100/1000-document SQL benchmark.
            # All writes are through restricted-tenant HTTPS on this marked clone.
            a96 = verify_full_taxonomy_quote_journey(
                owner=checkpoint_owner, organization=organization,
                outsider=outsider, session=second_session, factory=factory,
            )
            assert a96['catalog_skus'] == 183
            assert a96['taxonomy_categories'] == 40
            assert a96['manager_attested_skus'] == 4
            evidence_dir = ROOT / 'artifacts'
            if not evidence_dir.is_dir() or evidence_dir.is_symlink():
                raise RuntimeError("A96 output directory is unsafe")
            evidence_path = evidence_dir / f"a96-multicategory-{uuid.uuid4().hex}.json"
            with evidence_path.open('x', encoding='utf-8') as evidence_file:
                json.dump(a96, evidence_file, ensure_ascii=False, sort_keys=True, indent=2)
                evidence_file.write('\n')
            print('A96_MULTICATEGORY_EVIDENCE ' + str(evidence_path))
            print('A96_MULTICATEGORY_RESULT ' + json.dumps(a96, ensure_ascii=False))

            # Finally exercise the source-aware audit migration on this same
            # provenance-tagged clone. Earlier A45/A96 scenarios intentionally
            # require 0030/0031, so 0032 can only be installed after them.
            with checkpoint_owner.begin() as connection:
                assert connection.scalar(text(
                    "SELECT version_num FROM public.alembic_version"
                )) == "0031_catalog_taxonomy_v4"
                audit_migration = Config(str(ROOT / "backend" / "alembic.ini"))
                audit_migration.attributes["connection"] = connection
                command.upgrade(audit_migration, "0032_supplier_quote_audit")
                assert connection.scalar(text(
                    "SELECT version_num FROM public.alembic_version"
                )) == "0032_supplier_quote_audit"
            from canonical_signed_source_comparison_acceptance import (
                verify_signed_quote_after_audit_migration,
            )

            verify_signed_quote_after_audit_migration(
                organization, second_session, factory,
            )
            from canonical_supplier_registry_acceptance import (
                verify_retired_supplier_registry_https,
            )

            verify_retired_supplier_registry_https(
                owner=checkpoint_owner, organization=organization,
                session=second_session, app_factory=factory,
            )
            # A101: new independently reviewed XLSX source for the same
            # supplier. Run last: publishing changes only clone active version.
            from canonical_supplier_catalog_diff_acceptance import (
                verify_source_update_diff_https,
            )

            verify_source_update_diff_https(
                tenant=tenant, owner=checkpoint_owner, organization=organization,
                outsider=outsider, editor_id=second_actor, session=second_session,
                app_factory=factory,
            )
            from canonical_quote_document_acceptance import verify_ai_offer_approval_https

            # The earlier fault scenario intentionally revoked owner_session,
            # and this account has reached its tested login budget. Use a new
            # independently authenticated synthetic owner; do not revive tokens
            # or weaken the existing rate-limit acceptance.
            profile_actor = uuid.uuid4()
            profile_email = f"profile-owner-{nonce}@example.invalid"
            with checkpoint_owner.begin() as connection:
                connection.execute(text("""
                    INSERT INTO public.users(id,email,password_hash,status,email_verified_at)
                    VALUES(:id,:email,:password,'active',clock_timestamp())
                """), {"id": profile_actor, "email": profile_email, "password": hash_password(password)})
                connection.execute(text("""
                    INSERT INTO public.organization_memberships(organization_id,user_id,role,status)
                    VALUES(:org,:id,'owner','active')
                """), {"org": organization, "id": profile_actor})
            with auth.begin() as connection:
                profile_owner_session = AuthSessionRepository(connection).login(
                    profile_email, password, peer="127.0.0.3",
                )
            verify_ai_offer_approval_https(
                owner=checkpoint_owner, tenant=tenant, organization=organization, outsider=outsider,
                session=second_session, app_factory=factory,
                owner_session=profile_owner_session,
            )
            if os.environ.get('KP_DESK_E2E_FEEDBACK_STORAGE') == '1':
                from canonical_training_feedback_storage_acceptance import (
                    verify_feedback_storage_sql,
                )

                verify_feedback_storage_sql(checkpoint_owner, tenant)
                from canonical_feedback_storage_races import verify_feedback_storage_races

                verify_feedback_storage_races(checkpoint_owner, tenant, auth, organization,
                                              profile_owner_session, factory,
                                              ROOT / 'artifacts' / ('local-release-20261102'
                                                  if os.environ.get('KP_DESK_E2E_RELEASE_RUNTIME') == '1'
                                                  else 'ml-feedback-storage-20261102'))
            # The browser harness verifies and parses the authenticated PDF
            # before disposing its temporary directory and browser session.
            # A further login here would legitimately trip auth rate limits.
            if os.environ.get('KP_DESK_E2E_RELEASE_RUNTIME') == '1':
                from canonical_release_runtime_acceptance import verify_release_runtime

                verify_release_runtime(admin, _owner_engine)
        finally:
            checkpoint_owner.dispose()
    finally:
        try:
            for engine in engines:
                engine.dispose()
            if created:
                drop_test_clone(admin, name)
        finally:
            admin.dispose()


# Explicit offline accounting authority; production requires PostgreSQL.
pytestmark = pytest.mark.usefixtures("mock_llm_operation")
