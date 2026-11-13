"""Real tenant/PostgreSQL quote approval and final offer PDF on the A45 clone."""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import Engine, text

from kpdesk.api.routes import get_groq_client, get_groq_settings
from kpdesk.api.tenant_session_routes import SESSION_COOKIE
from kpdesk.db.auth_sessions import IssuedSession
from kpdesk.extraction.groq import GroqSettings


def verify_ai_offer_approval_https(
    *, owner: Engine, tenant: Engine, organization: uuid.UUID, outsider: uuid.UUID, session: IssuedSession,
    owner_session: IssuedSession,
    app_factory: Callable[[str], FastAPI],
) -> None:
    with owner.connect() as conn:
        row = conn.execute(text("""
            SELECT d.id,d.current_revision,r.snapshot_sha256,r.total_rub,r.catalog_version_id
            FROM public.quote_documents d JOIN public.quote_revisions r
              ON r.organization_id=d.organization_id AND r.quote_id=d.id
             AND r.number=d.current_revision
            WHERE d.organization_id=:org AND r.is_complete
              AND (r.snapshot->>'approved') IS DISTINCT FROM 'true'
              AND r.snapshot->'source_request' IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(r.snapshot->'lines') AS line
                              WHERE line->>'supplier_id' IS NOT NULL)
              AND EXISTS (SELECT 1 FROM jsonb_array_elements(r.snapshot->'lines') AS line
                          WHERE line->>'sku'='E2E-PAPER-B')
            ORDER BY d.created_at DESC LIMIT 1
        """), {"org": organization}).one()
    narrative = {
        "headline": "Решение для комплектации вашей заявки",
        "opening": "Уважаемые коллеги! Предлагаем рассмотреть решение по вашей заявке.",
        "solution": "В предложение включены товары из подтверждённого прайса. Состав подготовлен по заданным требованиям.",
        "closing": "Готовы обсудить состав заказа и согласовать условия поставки.",
    }
    first_bad_draft = True
    unsupported_property = 'Товары подходят для любых поверхностей.'

    async def provider() -> AsyncIterator[httpx.AsyncClient]:
        import json

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal first_bad_draft
            body = json.loads(request.content)
            for private in ('7736207543', '773601001', '1027700229193', 'Учебный адрес'):
                assert private not in request.content.decode('utf-8')
            schema = body['response_format']['json_schema']['name']
            payload: dict[str, object] = dict(narrative)
            if schema == 'kpdesk_proposal_fact_review_v2':
                payload = {name: {'status': 'supported', 'issues': []} for name in narrative}
            elif schema == 'kpdesk_commercial_proposal_revision_v2':
                payload = {name: narrative[name]
                           for name in body['response_format']['json_schema']['schema']['required']}
            if schema == 'kpdesk_commercial_proposal_v2' and first_bad_draft:
                first_bad_draft = False
                payload = {**narrative, 'solution': narrative['solution'] + ' ' + unsupported_property}
            elif schema == 'kpdesk_proposal_fact_review_v2':
                proposed = json.loads(body['messages'][-1]['content'])['draft']
                if unsupported_property in ''.join(part['text'] for part in proposed['solution']):
                    payload['solution'] = {'status': 'unsupported', 'issues': [
                        {'passage_id': 'solution:0', 'reason': 'unsupported_property'},
                    ]}
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
                                   'message': {'content': json.dumps(payload)}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            yield client

    app = app_factory("https://testserver")
    app.dependency_overrides[get_groq_client] = provider
    app.dependency_overrides[get_groq_settings] = lambda: GroqSettings(api_key="offline-clone-only")
    base = f"/api/session/organizations/{organization}/quotes/{row.id}"
    headers = {"Origin": "https://testserver", "X-CSRF-Token": session.csrf_token}
    context = {
        "issuer_name": "Учебная компания", "recipient_name": "Учебный заказчик",
        "manager_brief": "Подготовь презентацию решения в деловом стиле без выдуманных преимуществ.",
        "commercial_terms": "Условия поставки согласуются отдельно.",
    }
    body = {
        **context, "expected_revision": row.current_revision,
        "source_snapshot_sha256": row.snapshot_sha256,
        "consent_to_external_processing": True,
    }
    with TestClient(app, base_url="https://testserver") as client, TestClient(app, base_url="https://testserver") as manager:
        manager.cookies.set(SESSION_COOKIE, owner_session.session_token)
        manager_headers = {"Origin": "https://testserver", "X-CSRF-Token": owner_session.csrf_token}
        analysis_base = f"/api/session/organizations/{organization}/requests/analyze"
        analysis_body = {"extracted_request": {"lines": [{
            "source_quote": "20 коробок бумаги", "description": "Бумага", "category": "paper",
            "quantity": 20, "unit": "коробка", "brand": None, "paper_format": None,
            "paper_gsm": None, "pen_ink_color": None, "pen_ink_type": None, "attributes": {},
        }]}}
        assert client.post(analysis_base, json=analysis_body, headers=headers).status_code == 401
        assert client.post(base + "/presentation", json=body, headers=headers).status_code == 401
        matching_base = f"/api/session/organizations/{organization}/matches/batch-preview"
        matching_body = {
            "catalog_version_id": str(row.catalog_version_id), "mode": "tfidf",
            "requested_items": [{
                "description": "Бумага A4 80", "category": "paper", "quantity": 2,
                "unit": "пачка", "paper_format": "A4", "paper_gsm": 80,
            }] * 2,
        }
        assert client.post(matching_base, json=matching_body, headers=headers).status_code == 401
        client.cookies.set(SESSION_COOKIE, session.session_token)
        company_url = f"/api/session/organizations/{organization}/company-profile"
        company = client.get(company_url)
        assert company.status_code == 200 and company.headers["Cache-Control"] == "no-store"
        profile = company.json()["profile"]
        assert profile["revision"] == 1 and profile["data"]["contacts"] == "sales@example.invalid"
        assert profile["data"]["issuer_details"]["inn"] == "7736207543"
        assert profile["data"]["logo"]["width"] == 360
        logo_url = company_url + "/logo/prepare"
        logo_input = (Path(__file__).parent / "fixtures/company-logo.png").read_bytes()
        logo_file = {"file": ("synthetic.png", logo_input, "image/png")}
        assert client.post(logo_url, files=logo_file, headers=headers).status_code == 403
        assert manager.post(logo_url, files=logo_file, headers={"Origin": "https://testserver"}).status_code == 401
        assert manager.post(logo_url.replace(str(organization), str(outsider)), files=logo_file, headers=manager_headers).status_code == 403
        # Exercise the SECURITY DEFINER boundary without HTTP/Pydantic validation.
        from sqlalchemy import bindparam
        from sqlalchemy.dialects.postgresql import JSONB

        from kpdesk.db.authenticated_tenant import bind_authenticated_tenant

        with tenant.begin() as connection:
            bind_authenticated_tenant(connection, session_token=owner_session.session_token,
                                      organization_id=organization, operation="manage", csrf=owner_session.csrf_token)
            save_sql = text("SELECT public.kpdesk_company_profile_save(:org,:data,1)").bindparams(bindparam("data", type_=JSONB))
            malformed_profiles: tuple[object, ...] = (None, [], "wrong", 42)
            for malformed in malformed_profiles:
                assert connection.scalar(save_sql, {"org": organization, "data": malformed}) is None
            for invalid_logo in ({"sha256": "0" * 64}, {"width": 900}, {"height": True},
                                 {"mime_type": "image/svg+xml"}, {"url": "https://example.invalid/unsafe"},
                                 {"data_base64": "not base64"}):
                assert connection.scalar(save_sql, {"org": organization, "data": {
                    **profile["data"], "logo": {**profile["data"]["logo"], **invalid_logo},
                }}) is None
            for invalid in ({"inn": "7736207544"}, {"inn": "0000000000"}, {"kpp": 123},
                            {"inn": "0123456789"}, {"full_name": ""}, {"address": "x" * 501},
                            {"director": "not an allowed field"}):
                data = {**profile["data"], "issuer_details": {**profile["data"]["issuer_details"], **invalid}}
                assert connection.scalar(save_sql, {"org": organization, "data": data}) is None
            invalid_parties: tuple[object, ...] = (None, [], "wrong", {"inn": "7736207543"})
            for invalid_party in invalid_parties:
                assert connection.scalar(save_sql, {"org": organization, "data": {
                    **profile["data"], "issuer_details": invalid_party,
                }}) is None
            from kpdesk.company.requisites import PartyDetails

            sole_trader = PartyDetails(full_name="Учебный ИП", short_name="Учебный ИП", inn="500100732259")
            probe = connection.begin_nested()
            try:
                valid = connection.scalar(save_sql, {"org": organization, "data": {
                    **profile["data"], "issuer_details": sole_trader.model_dump(mode="json"),
                }})
                assert valid["revision"] == 2 and valid["data"]["issuer_details"]["inn"] == sole_trader.inn
            finally:
                probe.rollback()
        update_profile = {"data": profile["data"], "expected_revision": 1, "reviewed": True}
        assert client.put(company_url, json=update_profile, headers=headers).status_code == 403
        assert manager.put(company_url, json=update_profile, headers={"Origin": "https://testserver"}).status_code == 401
        assert client.get(company_url.replace(str(organization), str(outsider))).status_code == 403
        unreviewed = manager.put(company_url, json={**update_profile, "reviewed": False}, headers=manager_headers)
        assert unreviewed.status_code == 422, f"{unreviewed.status_code}: {unreviewed.text}"
        body["company_profile_revision"] = profile["revision"]
        assert client.post(analysis_base, json=analysis_body,
                           headers={"Origin": "https://testserver"}).status_code == 401
        assert client.post(analysis_base.replace(str(organization), str(outsider)),
                           json=analysis_body, headers=headers).status_code == 403
        analyzed = client.post(analysis_base, json=analysis_body, headers=headers)
        assert analyzed.status_code == 200, analyzed.text
        assert analyzed.headers["Cache-Control"] == "no-store"
        assert not analyzed.json()["ready_for_matching"]
        assert set(analyzed.json()["lines"][0]["missing_fields"]) == {"paper_format", "paper_gsm"}
        assert "плотность" in analyzed.json()["questions_text"]
        active = client.get(f"/api/session/organizations/{organization}/catalog/current")
        assert active.status_code == 200, active.text
        matching_body["catalog_version_id"] = active.json()["version_id"]
        assert client.post(matching_base, json=matching_body, headers={
            "Origin": "https://testserver",
        }).status_code == 401
        assert client.post(matching_base.replace(str(organization), str(outsider)),
                           json=matching_body, headers=headers).status_code == 403
        matched = client.post(matching_base, json=matching_body, headers=headers)
        assert matched.status_code == 200, matched.text
        assert matched.headers["Cache-Control"] == "no-store"
        assert [line["line_number"] for line in matched.json()["lines"]] == [1, 2]
        assert all(line["requires_manager_confirmation"] for line in matched.json()["lines"])
        assert all(candidate["requirements"] for line in matched.json()["lines"]
                   for candidate in line["candidates"])
        assert client.post(matching_base, json={
            **matching_body, "catalog_version_id": str(uuid.uuid4()),
        }, headers=headers).status_code == 409
        variants_url = matching_base.replace("batch-preview", "variants")
        assert client.post(variants_url, json=matching_body, headers={
            "Origin": "https://testserver",
        }).status_code == 401
        assert client.post(variants_url.replace(str(organization), str(outsider)),
                           json=matching_body, headers=headers).status_code == 403
        # A101 deliberately published a different final catalog. Use its real
        # facts for the positive case instead of assuming it still has A4 paper.
        with owner.connect() as connection:
            current_product = dict(connection.execute(text("""
                SELECT name AS description, category, sale_unit AS unit, brand,
                       paper_format, paper_gsm, pen_ink_color, pen_ink_type, attributes
                FROM public.catalog_items
                WHERE organization_id=:org AND catalog_version_id=:version
                ORDER BY sku LIMIT 1
            """), {"org": organization, "version": uuid.UUID(active.json()["version_id"])}).mappings().one())
        variant_body = {**matching_body, "requested_items": [{**current_product, "quantity": 2}] * 2}
        variants = client.post(variants_url, json=variant_body, headers=headers)
        assert variants.status_code == 200, variants.text
        assert variants.headers["Cache-Control"] == "no-store"
        assert variants.json()["candidate_limit_per_line"] == 20
        assert variants.json()["variants"][0]["selectable"]
        assert len(variants.json()["variants"][0]["lines"]) == 2
        assert client.post(variants_url, json={**variant_body,
            "catalog_version_id": str(uuid.uuid4()),
        }, headers=headers).status_code == 409
        forbidden = client.post(base.replace(str(organization), str(outsider)) + "/presentation", json=body, headers=headers)
        assert forbidden.status_code == 403
        without_consent = client.post(base + "/presentation", json={**body, "consent_to_external_processing": False}, headers=headers)
        assert without_consent.status_code == 422
        alternative_body = {"expected_revision": row.current_revision,
                            "source_snapshot_sha256": row.snapshot_sha256}
        assert client.post(base + "/alternatives", json=alternative_body,
                           headers={"Origin": "https://testserver"}).status_code == 401
        assert client.post(base.replace(str(organization), str(outsider)) + "/alternatives",
                           json=alternative_body, headers=headers).status_code == 403
        assert client.post(base + "/alternatives", json={**alternative_body,
            "source_snapshot_sha256": "0" * 64}, headers=headers).status_code == 409
        alternative_preview = client.post(base + "/alternatives", json=alternative_body, headers=headers)
        assert alternative_preview.status_code == 200, alternative_preview.text
        assert alternative_preview.headers["Cache-Control"] == "no-store"
        offered_alternatives = alternative_preview.json()["alternatives"]
        assert offered_alternatives
        body["alternative_selections"] = [offered_alternatives[0]["selection"]]
        with owner.connect() as connection:
            prior_attempts = set(connection.scalars(text(
                'SELECT id FROM public.tenant_llm_attempts WHERE organization_id=:org'
            ), {'org': organization}).all())
        generated = client.post(base + "/presentation", json=body, headers=headers)
        assert generated.status_code == 200, generated.text
        with owner.connect() as connection:
            attempts_after = connection.execute(text(
                'SELECT id,status FROM public.tenant_llm_attempts WHERE organization_id=:org'
            ), {'org': organization}).all()
            new_statuses = [status for attempt_id, status in attempts_after if attempt_id not in prior_attempts]
            assert len(new_statuses) == 4
            assert new_statuses.count('success') == 3 and new_statuses.count('failed') == 1
        assert generated.json()["narrative"] == narrative
        document = {**context, "issued_on": "2026-10-28", "narrative": generated.json()["narrative"]}
        document["company_profile"] = generated.json()["company_profile"]
        document["alternatives"] = generated.json()["alternatives"]
        document["appearance"] = {"template": "presentation", "accent_color": "#008B50", "show_company_section": True, "layout_version": 2}
        assert document["alternatives"] == offered_alternatives[:1]
        assert document["company_profile"] == profile
        changed_profile = manager.put(company_url, json={**update_profile, "data": {
            **profile["data"], "contacts": "snapshot@example.invalid",
        }}, headers=manager_headers)
        assert changed_profile.status_code == 200, changed_profile.text
        assert manager.put(company_url, json=update_profile, headers=manager_headers).status_code == 409
        approval_body = {
            "expected_revision": row.current_revision, "source_snapshot_sha256": row.snapshot_sha256,
            "document": document, "reviewed": True,
        }
        approval_headers = {**headers, "Idempotency-Key": "clone-document-approval"}
        assert client.post(base + "/approve", json=approval_body, headers=approval_headers).status_code == 409
        body["company_profile_revision"] = 2
        refreshed = manager.post(base + "/presentation", json=body, headers=manager_headers)
        assert refreshed.status_code == 200, refreshed.text
        document["company_profile"] = refreshed.json()["company_profile"]
        document["alternatives"] = refreshed.json()["alternatives"]
        approval_body["document"] = document
        no_csrf = client.post(base + "/approve", json=approval_body, headers={
            "Origin": "https://testserver", "Idempotency-Key": "missing-csrf-approval",
        })
        assert no_csrf.status_code == 401
        forged_price = client.post(base + "/approve", json={**approval_body, "total_rub": "0.01"}, headers=approval_headers)
        assert forged_price.status_code == 422
        bad_appearance = client.post(base + "/approve", json={**approval_body, "document": {
            **document, "appearance": {**document["appearance"], "accent_color": "url(https://example.invalid)"},
        }}, headers=approval_headers)
        assert bad_appearance.status_code == 422
        no_review = client.post(base + "/approve", json={**approval_body, "reviewed": False}, headers=approval_headers)
        assert no_review.status_code == 422
        tampered_alternative = {**document["alternatives"][0], "total_rub": "0.01"}
        tampered = client.post(base + "/approve", json={**approval_body, "document": {
            **document, "alternatives": [tampered_alternative],
        }}, headers=approval_headers)
        assert tampered.status_code == 422, tampered.text
        approved = client.post(base + "/approve", json=approval_body, headers=approval_headers)
        assert approved.status_code == 201, approved.text
        result = approved.json()
        assert result["approved"] and result["revision"] == row.current_revision + 1
        assert result["approval"]["user_id"] == str(session.user_id)
        assert result["total_rub"] == str(row.total_rub)
        assert all(line["requirements"] for line in result["lines"])
        assert result["document"]["alternatives"] == document["alternatives"]
        assert result["document"]["appearance"] == document["appearance"]
        from canonical_training_feedback_acceptance import verify_training_feedback

        verify_training_feedback(manager, client, base, result, manager_headers, headers, outsider, organization)
        approved_pdf_before_changes = client.get(base + "/pdf", params={"revision": result["revision"]}).content
        from test_company_logo import source_logo

        new_logo = manager.post(logo_url, files={"file": ("new.png", source_logo(color="blue"), "image/png")}, headers=manager_headers)
        assert new_logo.status_code == 200, new_logo.text
        assert manager.get(company_url).json()["profile"]["revision"] == 2
        later = manager.put(company_url, json={"expected_revision": 2, "reviewed": True,
            "data": {**profile["data"], "contacts": "later@example.invalid", "issuer_details": {
                **profile["data"]["issuer_details"], "address": "Поздний адрес отправителя",
            }, "logo": new_logo.json()["logo"]}}, headers=manager_headers)
        assert later.status_code == 200, later.text
        race_apps = [app_factory("https://testserver"), app_factory("https://testserver")]

        def save_competing_profile(index: int) -> int:
            with TestClient(race_apps[index], base_url="https://testserver") as competing:
                competing.cookies.set(SESSION_COOKIE, owner_session.session_token)
                result = competing.put(company_url, json={"expected_revision": 3, "reviewed": True,
                    "data": {**later.json()["profile"]["data"], "contacts": f"race-{index}@example.invalid"}}, headers=manager_headers)
                return int(result.status_code)

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(save_competing_profile, (0, 1))) == [200, 409]
        assert client.get(company_url).json()["profile"]["revision"] == 4
        replay = client.post(base + "/approve", json=approval_body, headers=approval_headers)
        assert replay.status_code == 201 and replay.json() == result
        stale = client.post(base + "/approve", json=approval_body, headers={**headers, "Idempotency-Key": "stale-approval"})
        assert stale.status_code == 409
        pdf = client.get(base + "/pdf", params={"revision": result["revision"]})
        assert pdf.status_code == 200
        assert pdf.content == approved_pdf_before_changes
        assert client.get(company_url).json()["profile"]["data"]["logo"]["sha256"] != result["document"]["company_profile"]["data"]["logo"]["sha256"]
        pdf_text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(pdf.content)).pages)
        assert "Уважаемые коллеги!" in pdf_text and "Учебный заказчик" in pdf_text
        assert "Вариант 2" in pdf_text and "Их суммы не складываются" in pdf_text
        assert "О компании" in pdf_text
        assert document["company_profile"]["data"]["about"] in pdf_text
        from decimal import Decimal

        from kpdesk.quotes.pdf_brand import money

        expected_total = money(Decimal(document["alternatives"][0]["total_rub"]))
        assert expected_total.replace("\u00a0", " ") in pdf_text.replace("\u00a0", " ")
        assert "snapshot@example.invalid" in pdf_text and "later@example.invalid" not in pdf_text
        assert "Реквизиты отправителя" in pdf_text and "7736207543" in pdf_text
        assert "Учебный адрес" in pdf_text and "Поздний адрес отправителя" not in pdf_text
        assert "Закупка" not in pdf_text and "Наценка" not in pdf_text and "Не утверждено" not in pdf_text
        # A45 calls this helper only inside its provenance-tagged synthetic clone.
        feedback_artifact = ('local-release-20261102' if os.environ.get('KP_DESK_E2E_RELEASE_RUNTIME') == '1'
                             else 'ml-feedback-storage-20261102' if os.environ.get('KP_DESK_E2E_FEEDBACK_STORAGE') == '1'
                             else 'ml-feedback-20261102')
        (Path(__file__).resolve().parents[2] / 'artifacts' / feedback_artifact / 'commercial-offer-options-clone.pdf').write_bytes(pdf.content)
        historical = client.get(base, params={"revision": row.current_revision}).json()
        assert historical["snapshot_sha256"] == row.snapshot_sha256 and not historical["approved"]
