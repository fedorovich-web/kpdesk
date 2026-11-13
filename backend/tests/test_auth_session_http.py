"""Unreleased cookie-session HTTP contract checked with real transactional PostgreSQL.

Do not include this router in the actual create_app, or any production build,
until production tenant/RLS transition and auth release gates are satisfied.
"""
from __future__ import annotations

import csv
import importlib
import io
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import URL

from kpdesk.api.tenant_matching_routes import router as tenant_matching_router
from kpdesk.api.tenant_quote_routes import unreleased_tenant_quote_router
from kpdesk.api.tenant_session_routes import (
    get_unreleased_auth_repository,
    get_unreleased_login_engine,
    unreleased_router,
)
from kpdesk.catalog.schema import COLUMNS_V2
from kpdesk.config import AppSettings
from kpdesk.db.auth_sessions import AuthSessionRepository, hash_password
from kpdesk.main import create_app


class _RollbackOnlyPreviewAuthEngine:
    """Test adapter: auth request gets its own savepoint, outer A3 DDL rolls back."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    @contextmanager
    def begin(self) -> Iterator[Connection]:
        # Emulate the separate authentication database role inside this
        # rollback-only DDL test. App endpoints remain under kpdesk_app.
        self.connection.execute(text("SET ROLE kpdesk_auth_test"))
        try:
            with self.connection.begin_nested():
                yield self.connection
        finally:
            self.connection.execute(text("SET ROLE kpdesk_app"))


def test_no_public_auth_session_endpoint_exposed_before_release() -> None:
    with TestClient(create_app(testing=True)) as client:
        assert client.post(
            "/api/session/login", json={
                "email": "fake@example.invalid", "password": "nothing",
            },
            headers={"Origin": "http://localhost:5173"},
        ).status_code == 404
        assert client.get("/api/session/me").status_code == 404
        assert client.get(
            f"/api/session/organizations/{uuid.uuid4()}/quotes/{uuid.uuid4()}"
        ).status_code == 404


def test_https_only_session_cookie_csrf_and_two_org_http_boundary() -> None:
    env = dotenv_values(Path(__file__).resolve().parents[2] / ".env")
    password = env.get("KP_DESK_DB_PASSWORD")
    assert isinstance(password, str) and len(password) >= 32
    engine = create_engine(URL.create(
        "postgresql+psycopg", username="kpdesk", password=password,
        host="127.0.0.1", port=int(env.get("KP_DESK_DB_PORT") or 55432),
        database="kpdesk_tenant_test",
    ))
    alice, org_a, org_b, version = (uuid.uuid4() for _ in range(4))
    url_a = f"/api/session/organizations/{org_a}/catalog/current"
    url_b = f"/api/session/organizations/{org_b}/catalog/current"
    origin = {"Origin": "http://localhost:5173"}
    try:
        with engine.connect() as conn:
            transaction = conn.begin()
            try:
                assert conn.scalar(text(
                    "SELECT version_num FROM alembic_version"
                )) == "0006_legacy_quarantine"
                with Operations.context(MigrationContext.configure(conn)):
                    for name in (
                        "0007_tenant_contract_rls",
                        "0008_supplier_review_rls",
                        "0009_auth_sessions",
                        "0010_auth_gateway",
                        "0011_auth_role_split",
                        "0012_session_bound_rls",
                    ):
                        importlib.import_module("migrations.versions." + name).upgrade()
                for org, name in ((org_a, "Org A"), (org_b, "Org B")):
                    conn.execute(text("""
                        INSERT INTO organizations(id,name,status)
                        VALUES (:id,:name,'active')
                    """), {"id": org, "name": name})
                conn.execute(text("""
                    INSERT INTO users(
                        id,email,password_hash,status,email_verified_at
                    ) VALUES (
                        :id,'alice@session.local',:password,'active',now()
                    )
                """), {
                    "id": alice,
                    "password": hash_password("Correct-Horse-Staple-After-2026"),
                })
                conn.execute(text("""
                    INSERT INTO organization_memberships(
                        organization_id,user_id,role,status
                    ) VALUES (:org,:user,'owner','active')
                """), {"org": org_a, "user": alice})
                conn.execute(text("""
                    INSERT INTO catalog_versions(
                        id,organization_id,source_sha256,original_filename,rows_count
                    ) VALUES (:version,:org,:sha,'a.csv',1)
                """), {"version": version, "org": org_a, "sha": "a" * 64})
                conn.execute(text("""
                    INSERT INTO catalog_active(organization_id,catalog_version_id)
                    VALUES (:org,:version)
                """), {"org": org_a, "version": version})
                conn.execute(text("GRANT USAGE ON SCHEMA public TO kpdesk_app"))
                for table in ("organizations", "users", "organization_memberships"):
                    assert conn.scalar(text("""
                        SELECT has_table_privilege('kpdesk_app', :table, 'SELECT')
                    """), {"table": "public." + table}) is False
                for table in (
                    "catalog_versions", "catalog_items", "idempotency_records",
                    "catalog_active", "quote_documents", "quote_revisions",
                    "quote_write_requests",
                ):
                    conn.exec_driver_sql(
                        f"GRANT SELECT ON TABLE public.{table} TO kpdesk_app"
                    )
                for table in (
                    "catalog_versions", "catalog_items", "idempotency_records",
                    "catalog_active", "quote_documents", "quote_revisions",
                    "quote_write_requests",
                ):
                    conn.exec_driver_sql(
                        f"GRANT INSERT ON TABLE public.{table} TO kpdesk_app"
                    )
                conn.execute(text(
                     "GRANT UPDATE ON TABLE catalog_active, quote_documents TO kpdesk_app"
                ))
                conn.execute(text("SET ROLE kpdesk_app"))
                service = AuthSessionRepository(conn)
                app = create_app(
                    testing=True, settings=AppSettings(enable_synthetic_ml=True),
                )
                app.include_router(unreleased_router)
                app.include_router(unreleased_tenant_quote_router)
                app.include_router(tenant_matching_router)
                app.dependency_overrides[get_unreleased_auth_repository] = lambda: service
                auth_test_engine = _RollbackOnlyPreviewAuthEngine(conn)
                app.dependency_overrides[get_unreleased_login_engine] = (
                    lambda: auth_test_engine
                )
                with TestClient(
                    app, base_url="https://testserver",
                    client=("127.0.0.1", 50123),
                ) as client:
                    missing = client.get("/api/session/me")
                    assert missing.status_code == 401
                    assert missing.json()["code"] == "invalid_session"
                    wrong = client.post(
                        "/api/session/login", headers=origin, json={
                            "email": "alice@session.local",
                            "password": "incorrect password",
                        },
                    )
                    assert wrong.status_code == 401
                    assert wrong.json()["code"] == "invalid_login"
                    # The HTTP 401 was raised AFTER the login savepoint
                    # completed: the failed attempt must remain inside the
                    # caller-owned outer test transaction.
                    conn.execute(text("RESET ROLE"))
                    assert conn.scalar(text("""
                        SELECT sum(attempts) FROM auth_login_limits
                    """)) == 2
                    conn.execute(text("SET ROLE kpdesk_app"))
                    login = client.post(
                        "/api/session/login", headers=origin, json={
                            "email": "alice@session.local",
                            "password": "Correct-Horse-Staple-After-2026",
                        },
                    )
                    assert login.status_code == 200, login.text
                    assert "csrf_token" in login.json()
                    assert "session_token" not in login.text
                    assert login.headers["cache-control"] == "no-store"
                    set_cookie = login.headers["set-cookie"]
                    assert "__Host-kpdesk_session=" in set_cookie
                    assert "Secure" in set_cookie
                    assert "HttpOnly" in set_cookie
                    assert "SameSite=strict" in set_cookie
                    assert "Path=/" in set_cookie
                    assert "Max-Age=604800" in set_cookie
                    resumed = client.get("/api/session/resume")
                    assert resumed.status_code == 200, resumed.text
                    assert resumed.headers["cache-control"] == "no-store"
                    assert resumed.json() == {
                        "user_id": str(alice),
                        "csrf_token": login.json()["csrf_token"],
                    }
                    assert client.get("/api/session/me").json() == {
                        "user_id": str(alice),
                    }
                    active = client.get(url_a)
                    assert active.status_code == 200, active.text
                    assert active.json()["version_id"] == str(version)
                    assert active.json()["rows_count"] == 1
                    foreign = client.get(url_b)
                    assert foreign.status_code == 403
                    assert foreign.json()["code"] == "organization_forbidden"
                    catalog_csv = io.StringIO(newline="")
                    writer = csv.writer(catalog_csv, delimiter=";", lineterminator="\n")
                    writer.writerow(COLUMNS_V2)
                    products = {
                        "sku": "A-PAPER-001", "name": "Office paper A4 80",
                        "category": "paper", "sale_unit": "\u043f\u0430\u0447\u043a\u0430",
                        "pieces_per_sale_unit": 1, "price_rub": "429.90",
                        "paper_format": "A4", "paper_gsm": 80,
                        "attributes_json": "{}",
                    }
                    writer.writerow([products.get(col, "") for col in COLUMNS_V2])
                    raw_csv = catalog_csv.getvalue().encode("utf-8")
                    url_import_a = url_a.replace("/current", "/import")
                    bad_import = client.post(
                        url_import_a, headers={
                            **origin, "Idempotency-Key": "release-preview-001",
                        },
                        files={"file": ("paper.csv", raw_csv, "text/csv")},
                    )
                    assert bad_import.status_code == 401
                    permitted = client.post(
                        url_import_a,
                        headers={
                            **origin, "Idempotency-Key": "release-preview-001",
                            "X-CSRF-Token": login.json()["csrf_token"],
                        },
                        files={"file": ("paper.csv", raw_csv, "text/csv")},
                    )
                    assert permitted.status_code == 201, permitted.text
                    latest = permitted.json()
                    assert latest["rows_count"] == 1
                    assert latest["version_id"] != str(version)
                    assert client.get(url_a).json()["version_id"] == latest["version_id"]
                    replay = client.post(
                        url_import_a,
                        headers={
                            **origin, "Idempotency-Key": "release-preview-001",
                            "X-CSRF-Token": login.json()["csrf_token"],
                        },
                        files={"file": ("paper.csv", raw_csv, "text/csv")},
                    )
                    assert replay.status_code == 201
                    assert replay.json()["version_id"] == latest["version_id"]
                    foreign_import = client.post(
                        url_b.replace("/current", "/import"),
                        headers={
                            **origin, "Idempotency-Key": "release-preview-001",
                            "X-CSRF-Token": login.json()["csrf_token"],
                        },
                        files={"file": ("paper.csv", raw_csv, "text/csv")},
                    )
                    assert foreign_import.status_code == 403
                    match_url = f"/api/session/organizations/{org_a}/matches/preview"
                    ml_body = {
                        "catalog_version_id": latest["version_id"],
                        "requested_item": {
                            "description": "Office paper A4 80",
                            "category": "paper", "quantity": 2,
                            "unit": "пачка",
                            "paper_format": "A4", "paper_gsm": 80,
                        },
                    }
                    match_headers = {
                        **origin, "X-CSRF-Token": login.json()["csrf_token"],
                    }
                    no_csrf_match = client.post(
                        match_url, headers=origin, json=ml_body,
                    )
                    assert no_csrf_match.status_code == 401
                    foreign_match = client.post(
                        f"/api/session/organizations/{org_b}/matches/preview",
                        headers=match_headers, json=ml_body,
                    )
                    assert foreign_match.status_code == 403
                    stale_match = client.post(
                        match_url, headers=match_headers, json={
                            **ml_body, "catalog_version_id": str(version),
                        },
                    )
                    assert stale_match.status_code == 409
                    assert stale_match.json()["code"] == "stale_catalog"
                    matched = client.post(
                        match_url, headers=match_headers, json=ml_body,
                    )
                    assert matched.status_code == 200, matched.text
                    match_result = matched.json()
                    assert match_result["catalog_version_id"] == latest["version_id"]
                    assert match_result["requires_manager_confirmation"] is True
                    assert match_result["calibrated_no_match"] is False
                    assert [item["sku"] for item in match_result["candidates"]] == [
                        "A-PAPER-001",
                    ]
                    assert match_result["candidates"][0]["price_rub"] == "429.90"
                    no_product = client.post(
                        match_url, headers=match_headers, json={
                            **ml_body,
                            "requested_item": {
                                "description": "Wireless computer mouse",
                                "category": "mouse",
                            },
                        },
                    )
                    assert no_product.status_code == 200, no_product.text
                    assert no_product.json()["candidates"] == []
                    suggested = client.post(
                        match_url.replace("/preview", "/category-suggestion"),
                        headers=match_headers,
                        json={"description": "Mouse pad", "original_category": "mouse"},
                    )
                    assert suggested.status_code == 200, suggested.text
                    assert suggested.json()["requires_explicit_acceptance"] is True

                    # A9 tenant quote HTTP routes: same-RLS transaction and
                    # the same authenticated catalog snapshot. Neither buyer
                    # descriptions nor HTTP parameters supply sale prices.
                    quote_url = f"/api/session/organizations/{org_a}/quotes"
                    quote_body = {
                        "catalog_version_id": latest["version_id"],
                        "extracted_request": {"lines": [{
                            "source_quote": "Office paper A4, 2 packs",
                            "description": "Office paper A4 80",
                            "category": "paper", "quantity": 2,
                            "unit": "\u043f\u0430\u0447\u043a\u0430",
                            "brand": None, "paper_format": "A4",
                            "paper_gsm": 80, "pen_ink_color": None,
                            "pen_ink_type": None, "attributes": {},
                        }]},
                    }
                    wrong_quote = client.post(
                        quote_url, headers={
                            **origin, "Idempotency-Key": "quote-a-1",
                        }, json=quote_body,
                    )
                    assert wrong_quote.status_code == 401
                    quote_headers = {
                        **origin, "Idempotency-Key": "quote-a-1",
                        "X-CSRF-Token": login.json()["csrf_token"],
                    }
                    stored_quote = client.post(
                        quote_url, headers=quote_headers, json=quote_body,
                    )
                    assert stored_quote.status_code == 201, stored_quote.text
                    created_quote = stored_quote.json()
                    assert created_quote["revision"] == 1
                    assert created_quote["catalog_version_id"] == latest["version_id"]
                    assert created_quote["approved"] is False
                    assert created_quote["ready_for_draft"] is True
                    assert created_quote["total_rub"] == "859.80"
                    assert created_quote["lines"][0]["unit_price_rub"] == "429.90"
                    assert stored_quote.headers["cache-control"] == "no-store"
                    explicit_selected = client.post(
                        quote_url, headers={
                            **quote_headers, "Idempotency-Key": "quote-a-ml-selected",
                        }, json={
                            **quote_body,
                            "manager_selections": [{
                                "line_number": 1, "sku": "A-PAPER-001",
                            }],
                        },
                    )
                    assert explicit_selected.status_code == 201, explicit_selected.text
                    approved_line = explicit_selected.json()["lines"][0]
                    assert approved_line["status"] == "manager_selected"
                    assert approved_line["sku"] == "A-PAPER-001"
                    assert approved_line["unit_price_rub"] == "429.90"
                    assert explicit_selected.json()["total_rub"] == "859.80"
                    reopened_selected = client.get(
                        f'{quote_url}/{explicit_selected.json()["quote_id"]}',
                    )
                    assert reopened_selected.json() == explicit_selected.json()
                    selected_pdf = client.get(
                        f'{quote_url}/{explicit_selected.json()["quote_id"]}/pdf',
                    )
                    assert selected_pdf.status_code == 200, selected_pdf.text
                    assert selected_pdf.content.startswith(b"%PDF")
                    invalid_selected = client.post(
                        quote_url, headers={
                            **quote_headers, "Idempotency-Key": "quote-a-ml-forged",
                        }, json={
                            **quote_body,
                            "manager_selections": [{
                                "line_number": 1, "sku": "OTHER-ORG-SKU",
                            }],
                        },
                    )
                    assert invalid_selected.status_code == 422
                    assert invalid_selected.json()["code"] == "invalid_quote"
                    quote_id = created_quote["quote_id"]
                    retrieved = client.get(f"{quote_url}/{quote_id}")
                    assert retrieved.status_code == 200
                    assert retrieved.json() == created_quote
                    historical = client.get(f"{quote_url}/{quote_id}?revision=1")
                    assert historical.json() == created_quote
                    replay_quote = client.post(
                        quote_url, headers=quote_headers, json=quote_body,
                    )
                    assert replay_quote.status_code == 201
                    assert replay_quote.json() == created_quote
                    changed_quote_body = {
                        **quote_body,
                        "extracted_request": {"lines": [
                            {**quote_body["extracted_request"]["lines"][0],
                             "quantity": 3},
                        ]},
                    }
                    key_reused = client.post(
                        quote_url, headers=quote_headers, json=changed_quote_body,
                    )
                    assert key_reused.status_code == 409
                    assert key_reused.json()["code"] == "idempotency_conflict"
                    foreign_quote = client.get(
                        f"/api/session/organizations/{org_b}/quotes/{quote_id}"
                    )
                    assert foreign_quote.status_code == 403
                    assert foreign_quote.json()["code"] == "organization_forbidden"
                    revised_quote = client.post(
                        f"{quote_url}/{quote_id}/revisions",
                        headers={**quote_headers, "Idempotency-Key": "quote-a-rev"},
                        json={**changed_quote_body, "expected_revision": 1},
                    )
                    assert revised_quote.status_code == 201, revised_quote.text
                    assert revised_quote.json()["revision"] == 2
                    assert revised_quote.json()["total_rub"] == "1289.70"
                    assert client.get(f"{quote_url}/{quote_id}?revision=1").json() == (
                        created_quote
                    )
                    assert client.get(f"{quote_url}/{quote_id}").json() == (
                        revised_quote.json()
                    )
                    stale_quote = client.post(
                        f"{quote_url}/{quote_id}/revisions",
                        headers={**quote_headers, "Idempotency-Key": "quote-a-stale"},
                        json={**changed_quote_body, "expected_revision": 1},
                    )
                    assert stale_quote.status_code == 409
                    assert stale_quote.json()["code"] == "stale_revision"
                    pdf = client.get(f"{quote_url}/{quote_id}/pdf?revision=1")
                    assert pdf.status_code == 200, pdf.text
                    assert pdf.content.startswith(b"%PDF")
                    assert pdf.headers["cache-control"] == "no-store"
                    missing_csrf = client.post(
                        "/api/session/logout", headers=origin,
                    )
                    assert missing_csrf.status_code == 401
                    assert client.get("/api/session/me").status_code == 200
                    rejected_origin = client.post(
                        "/api/session/logout",
                        headers={
                            "Origin": "https://attacker.invalid",
                            "X-CSRF-Token": login.json()["csrf_token"],
                        },
                    )
                    assert rejected_origin.status_code == 403
                    logout = client.post(
                        "/api/session/logout",
                        headers={
                            **origin,
                            "X-CSRF-Token": login.json()["csrf_token"],
                        },
                    )
                    assert logout.status_code == 204
                    assert logout.headers["cache-control"] == "no-store"
                    assert client.get("/api/session/me").status_code == 401
                    assert client.get("/api/session/resume").status_code == 401
                    assert conn.scalar(text("SELECT current_user")) == "kpdesk_app"
                conn.execute(text("RESET ROLE"))
            finally:
                transaction.rollback()
        with engine.connect() as verify:
            assert verify.scalar(text(
                "SELECT version_num FROM alembic_version"
            )) == "0006_legacy_quarantine"
            assert verify.scalar(text(
                "SELECT to_regclass('public.auth_sessions')"
            )) is None
            assert verify.scalar(text(
                "SELECT to_regprocedure('public.kpdesk_user_organizations(text)')"
            )) is None
            assert verify.scalar(text(
                "SELECT count(*) FROM users WHERE id=:id"
            ), {"id": alice}) == 0
    finally:
        engine.dispose()
