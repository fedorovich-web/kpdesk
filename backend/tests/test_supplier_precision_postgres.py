"""Isolated-clone-only PostgreSQL persistence for supplier price-precision attestations."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import secrets
import subprocess
import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from httpx import Response as HttpResponse
from postgres_clone_connection import drop_legacy_test_clone as drop_test_clone
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError, IntegrityError

from kpdesk.api.tenant_session_routes import SESSION_COOKIE, get_unreleased_auth_repository
from kpdesk.catalog.supplier_canonical_preview import ReviewedPrecisionPrice, source_row_sha256
from kpdesk.catalog.supplier_inference import inspect_supplier_source
from kpdesk.catalog.supplier_mapping_storage import TenantSupplierMappingStore
from kpdesk.catalog.supplier_precision_storage import TenantSupplierPrecisionReviewStore
from kpdesk.catalog.supplier_profiles import (
    SupplierSchemaDrift,
    approve_supplier_profile,
    reuse_supplier_profile,
)
from kpdesk.catalog.supplier_staging import SupplierMapping, stage_supplier_prices
from kpdesk.db.auth_sessions import AuthSessionRepository
from kpdesk.db.tenant_access import AuthenticatedUser, TenantAccessDenied
from kpdesk.main import create_app

ROOT = Path(__file__).resolve().parents[2]
MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _engine(database: str, *, autocommit: bool = False) -> Engine:
    settings = dotenv_values(ROOT / ".env")
    password = settings.get("KP_DESK_DB_PASSWORD")
    assert isinstance(password, str) and len(password) >= 32
    engine = create_engine(URL.create(
        "postgresql+psycopg", username="kpdesk", password=password,
        host="127.0.0.1", port=int(settings.get("KP_DESK_DB_PORT") or 55432),
        database=database,
    ), isolation_level="AUTOCOMMIT" if autocommit else None)
    return engine


@pytest.mark.skipif(
    os.environ.get("KP_DESK_RUN_ISOLATED_PRECISION_PG") != "1",
    reason="Explicit opt-in to create and remove a source-templated isolated PostgreSQL clone",
)
def test_a22_precision_reviews_postgresql_clone_only() -> None:
    admin = _engine("postgres", autocommit=True)
    nonce = secrets.token_hex(6)
    name = f"kpdesk_accounts_http_{nonce}"
    assert re.fullmatch(r"kpdesk_accounts_http_[0-9a-f]{12}", name)
    org = uuid.uuid4()
    actor = uuid.uuid4()
    created = False
    try:
        source = _engine("kpdesk")
        try:
            with source.connect() as connection:
                assert connection.scalar(text("SELECT count(*) FROM users")) == 0
                assert connection.scalar(text("SELECT count(*) FROM organizations")) == 0
        finally:
            source.dispose()
        with admin.connect() as c:
            c.exec_driver_sql(f'CREATE DATABASE "{name}" TEMPLATE template0 OWNER kpdesk')
            created = True
            c.exec_driver_sql(
                f"COMMENT ON DATABASE \"{name}\" IS 'kpdesk:accounts-http:{nonce}'"
            )
        with subprocess.Popen(
            ["docker", "exec", "kpdesk-db-1", "pg_dump", "-U", "kpdesk",
             "-d", "kpdesk", "-Fc", "--no-owner"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ) as backup:
            assert backup.stdout is not None
            restored = subprocess.run(
                ["docker", "exec", "-i", "kpdesk-db-1", "pg_restore",
                 "-U", "kpdesk", "-d", name, "--exit-on-error", "--no-owner"],
                stdin=backup.stdout, capture_output=True, check=False, timeout=120,
            )
            backup.stdout.close()
            assert backup.wait(timeout=120) == 0, "Source pg_dump failed."
            assert restored.returncode == 0, restored.stderr.decode(
                "utf-8", errors="replace",
            )[-500:]
        clone = _engine(name)
        try:
            with clone.connect() as conn:
                transaction = conn.begin()
                try:
                    assert conn.scalar(text("SELECT current_database()")) == name
                    current_revision = conn.scalar(
                        text("SELECT version_num FROM alembic_version")
                    )
                    if current_revision == "0020_quote_review_audit":
                        migration = importlib.import_module(
                            "migrations.versions.0021_supplier_precision_reviews"
                        )
                        with Operations.context(MigrationContext.configure(conn)):
                            migration.upgrade()
                        conn.execute(text("""
                            UPDATE alembic_version SET version_num='0021_supplier_precision_reviews'
                        """))
                    elif current_revision not in (
                        "0021_supplier_precision_reviews", "0022_supplier_mapping_profiles",
                        "0023_supplier_mapping_history", "0024_supplier_unit_reviews", "0025_request_candidate_audit", "0026_request_source_restrictions", "0027_candidate_adjudications", "0028_quote_manager_decisions", "0029_manager_decisions_read", "0030_tenant_llm_cache", "0031_catalog_taxonomy_v4",
                    ):
                        pytest.fail("Unexpected release-clone Alembic revision.")
                    mapping_version = conn.scalar(
                        text("SELECT version_num FROM alembic_version")
                    )
                    if mapping_version == "0021_supplier_precision_reviews":
                        mapping_migration = importlib.import_module(
                            "migrations.versions.0022_supplier_mapping_profiles"
                        )
                        # Apply only on the marked, disposable rollback-only clone.
                        with Operations.context(MigrationContext.configure(conn)):
                            mapping_migration.upgrade()
                    assert conn.scalar(text(
                        "SELECT to_regclass('public.tenant_supplier_mapping_profiles')"
                    )) is not None
                    # A clone of an already-migrated working DB must not
                    # reapply append-only DDL or invent a downgrade. In both
                    # cases, exercise the identical PostgreSQL contract.
                    assert conn.scalar(text(
                        "SELECT to_regclass('public.tenant_supplier_precision_reviews')"
                    )) is not None
                    assert conn.execute(text("""
                        SELECT relrowsecurity,relforcerowsecurity
                        FROM pg_class WHERE oid=to_regclass('public.tenant_supplier_precision_reviews')
                    """)).one() == (True, True)
                    assert conn.scalar(text("""
                        SELECT has_table_privilege(
                            'kpdesk_tenant_runtime',
                            'public.tenant_supplier_precision_reviews','INSERT'
                        )
                    """)) is False
                    assert conn.scalar(text("""
                        SELECT has_function_privilege(
                            'kpdesk_tenant_runtime',
                            'public.kpdesk_supplier_precision_attest(uuid,text,text,integer,text,text,numeric,text,text,numeric,text)',
                            'EXECUTE'
                        )
                    """)) is True
                    assert conn.execute(text("""
                        SELECT public.kpdesk_precision_cents(10.125,'half_up'),
                               public.kpdesk_precision_cents(10.125,'half_even'),
                               public.kpdesk_precision_cents(10.135,'half_even'),
                               public.kpdesk_precision_cents(10.121,'down'),
                               public.kpdesk_precision_cents(10.121,'up')
                    """)).one() == (
                        Decimal("10.13"), Decimal("10.12"), Decimal("10.14"),
                        Decimal("10.12"), Decimal("10.13"),
                    )
                    assert conn.scalar(text("""
                        SELECT public.kpdesk_precision_source_numeric(
                            '1 250,045 RUB'
                        )
                    """)) == Decimal("1250.045")
                    assert conn.scalar(text("""
                        SELECT public.kpdesk_precision_source_numeric(
                            '1e4'
                        )
                    """)) is None
                    conn.execute(text("""
                        INSERT INTO organizations(id,name,status)
                        VALUES(:org,'Source precision test','active')
                    """), {"org": org})
                    conn.execute(text("""
                        INSERT INTO users(id,email,status,email_verified_at)
                        VALUES(:actor,:email,'active',clock_timestamp())
                    """), {"actor": actor, "email": f"test-{nonce}@invalid.local"})
                    # There must be NO way for unauthenticated SQL to forge review evidence.
                    conn.execute(text("SET ROLE kpdesk_tenant_runtime"))
                    assert conn.scalar(text("""
                        SELECT public.kpdesk_supplier_precision_attest(
                            :org, 'supplier', :source_hash, 2, :row_hash,
                            '10.125', 10.125::numeric, 'approve_rounding',
                            'half_up', 10.13::numeric,
                            'This review has deliberately not been authenticated.'
                        )
                    """), {"org": org, "source_hash": "a" * 64, "row_hash": "b" * 64}) is False
                    with pytest.raises(DBAPIError), conn.begin_nested():
                        conn.execute(text("""
                            INSERT INTO public.tenant_supplier_precision_reviews(
                                organization_id,supplier_id,source_sha256,source_line,
                                source_row_sha256,source_price_raw,source_price_exact,
                                finding,rounding_mode,rounded_price_rub,
                                rationale,reviewer_user_id
                            ) VALUES(
                                :org,'supplier',:source_hash,2,:row_hash,'10.125',
                                10.125,'approve_rounding','half_up',10.13,
                                'Direct INSERT must be denied',:actor
                            )
                        """), {
                            "org": org, "source_hash": "a" * 64,
                            "row_hash": "b" * 64, "actor": actor,
                        })
                    conn.execute(text("RESET ROLE"))
                    # Isolated clone: valid active membership authenticates an
                    # actual reviewer, not an arbitrary JSON reviewer string.
                    conn.execute(text("""
                        INSERT INTO organization_memberships(
                            organization_id,user_id,role,status
                        ) VALUES(:org,:actor,'owner','active')
                    """), {"org": org, "actor": actor})
                    session_token = secrets.token_urlsafe(32)
                    csrf_token = secrets.token_urlsafe(32)
                    digest = hashlib.sha256(session_token.encode("ascii")).hexdigest()
                    csrf_digest = hashlib.sha256(csrf_token.encode("ascii")).hexdigest()
                    conn.execute(text("""
                        INSERT INTO auth_sessions(
                            token_sha256,user_id,csrf_sha256,expires_at
                        ) VALUES(:digest,:actor,:csrf,clock_timestamp()+interval '1 hour')
                    """), {"digest": digest, "actor": actor, "csrf": csrf_digest})
                    conn.execute(text("SET ROLE kpdesk_tenant_runtime"))
                    conn.execute(text(
                        "SELECT set_config('kpdesk.session_sha256',:digest,true)"
                    ), {"digest": digest})
                    params = {"org": org, "source_hash": "a" * 64, "row_hash": "b" * 64}
                    assert conn.scalar(text("""
                        SELECT public.kpdesk_supplier_precision_attest(
                            :org,'supplier',:source_hash,2,:row_hash,'10.125',
                            10.125::numeric,'approve_rounding','half_up',
                            10.13::numeric,
                            'Human session verified this exact source amount and rule.'
                        )
                    """), params) is True
                    # Exact replay is idempotent and cannot alter any audit row.
                    assert conn.scalar(text("""
                        SELECT public.kpdesk_supplier_precision_attest(
                            :org,'supplier',:source_hash,2,:row_hash,'10.125',
                            10.125::numeric,'approve_rounding','half_up',
                            10.13::numeric,
                            'Human session verified this exact source amount and rule.'
                        )
                    """), params) is True
                    # A conflicting replay and a forged source-price decimal fail.
                    assert conn.scalar(text("""
                        SELECT public.kpdesk_supplier_precision_attest(
                            :org,'supplier',:source_hash,2,:row_hash,'10.125',
                            10.125::numeric,'needs_supplier_confirmation',NULL,
                            NULL,'Cannot silently overwrite a reviewed price.'
                        )
                    """), params) is False
                    assert conn.scalar(text("""
                        SELECT public.kpdesk_supplier_precision_attest(
                            :org,'supplier',:source_hash,3,:row_hash,'10.125',
                            10.126::numeric,'approve_rounding','half_up',
                            10.13::numeric,
                            'Wrong original numeric value does not match original price.'
                        )
                    """), params) is False
                    assert conn.scalar(text("""
                        SELECT public.kpdesk_supplier_precision_attest(
                            :org,'supplier',NULL,3,:row_hash,'10.125',
                            10.125::numeric,'approve_rounding','half_up',
                            10.13::numeric,
                            'A null hash must be rejected with a normal false result.'
                        )
                    """), params) is False
                    assert conn.scalar(text("""
                        SELECT count(*) FROM public.kpdesk_supplier_precision_get(
                            :org,'supplier',:source_hash
                        )
                    """), params) == 1
                    staged = stage_supplier_prices(
                        "current-owner.csv",
                        b"sku;name;price;currency;unit;pack\n"
                        b"S2;Office product;9.995;RUB;pcs;1\n",
                        SupplierMapping(
                            sku=0,name=1,price=2,currency=3,
                            sale_unit=4,pieces_per_sale_unit=5,header_row=1,
                        ),
                    )
                    mismatch_store = TenantSupplierPrecisionReviewStore(
                        conn, AuthenticatedUser(uuid.uuid4()), org,
                        session_token=session_token, csrf_token=csrf_token,
                    )
                    with pytest.raises(TenantAccessDenied):
                        mismatch_store.list_reviews("supplier", staged.sha256)
                    assert conn.scalar(text(
                        "SELECT current_setting('kpdesk.session_sha256',true)"
                    )) in ("", None)
                    mapping_original = (
                        b"sku;name;price;currency;unit\n"
                        b"009;Cable;15.00;RUB;pcs\n"
                    )
                    mapping_updated = (
                        b"sku;name;price;currency;unit\n"
                        b"009;Cable;17.00;RUB;pcs\n"
                    )
                    mapping_inspection = inspect_supplier_source(
                        "profile.csv", mapping_original,
                    )
                    mapping_profile = approve_supplier_profile(
                        mapping_inspection,
                        organization_id=org,
                        supplier_id="supplier",
                        reviewer=AuthenticatedUser(actor),
                        source_sha256=mapping_inspection.source_sha256,
                        selected_columns=mapping_inspection.proposed_fields,
                        price_basis="standard",
                        confirmed_currency=None,
                        review_evidence="Operator confirmed the supplier price column",
                        explicitly_confirmed=True,
                    )
                    mapping_store = TenantSupplierMappingStore(
                        conn, AuthenticatedUser(actor), org,
                        session_token=session_token, csrf_token=csrf_token,
                    )
                    mapping_store.save(mapping_profile)
                    mapping_store.save(mapping_profile)
                    saved_profile = mapping_store.latest(
                        "supplier", mapping_inspection,
                    )
                    assert saved_profile == mapping_profile
                    # Direct SELECT remains forbidden for tenant runtime;
                    # only the session-checked SQL accessor can read this audit.
                    with pytest.raises(DBAPIError), conn.begin_nested():
                        conn.scalar(text(
                            "SELECT count(*) FROM public.tenant_supplier_mapping_profiles"
                        ))
                    changed_inspection = inspect_supplier_source(
                        "profile.csv", mapping_updated,
                    )
                    assert mapping_store.latest("supplier", changed_inspection) == mapping_profile
                    reused = reuse_supplier_profile(
                        mapping_profile, changed_inspection,
                        "profile.csv", mapping_updated,
                        organization_id=org, supplier_id="supplier",
                    )
                    assert str(reused.staging.rows[0].price_rub) == "17.00"
                    changed_headers = inspect_supplier_source(
                        "profile.csv", mapping_updated.replace(b"price;", b"wholesale;"),
                    )
                    assert mapping_store.latest("supplier", changed_headers) is None
                    with pytest.raises(SupplierSchemaDrift):
                        reuse_supplier_profile(
                            mapping_profile, changed_headers,
                            "profile.csv", mapping_updated.replace(
                                b"price;", b"wholesale;",
                            ),
                            organization_id=org, supplier_id="supplier",
                        )
                    foreign_mapping = TenantSupplierMappingStore(
                        conn, AuthenticatedUser(actor), uuid.uuid4(),
                        session_token=session_token, csrf_token=csrf_token,
                    )
                    with pytest.raises(TenantAccessDenied):
                        foreign_mapping.latest("supplier", mapping_inspection)
                    assert conn.scalar(text("""
                        SELECT has_table_privilege(
                            'kpdesk_tenant_runtime',
                            'public.tenant_supplier_mapping_profiles','INSERT'
                        )
                    """)) is False
                    assert conn.scalar(text("""
                        SELECT has_function_privilege(
                            'kpdesk_tenant_runtime',
                            'public.kpdesk_supplier_mapping_save(uuid,text,jsonb)','EXECUTE'
                        )
                    """)) is True
                    store = TenantSupplierPrecisionReviewStore(
                        conn, AuthenticatedUser(actor), org,
                        session_token=session_token, csrf_token=csrf_token,
                    )
                    assert store.attest(
                        staged, "supplier", staged.rows[0],
                        finding="approve_rounding",
                        precision_review=ReviewedPrecisionPrice(
                            price_rub=Decimal("10.00"),
                            rounding_mode="half_up",
                            explanation=(
                                "Source price rounding was independently reviewed "
                                "under this active session."
                            ),
                        ),
                        rationale=(
                            "Source price rounding was independently reviewed "
                            "under this active session."
                        ),
                    )
                    assert len(store.list_reviews("supplier", staged.sha256)) == 1
                    assert len(store.list_reviews("supplier", "a" * 64)) == 1
                    # HTTP is bound to this isolated transaction: the public
                    # working database cannot receive any test records.
                    def isolated_repository() -> Iterator[AuthSessionRepository]:
                        yield AuthSessionRepository(conn)

                    api = create_app(testing=True, tenant_sessions=True)
                    api.dependency_overrides[get_unreleased_auth_repository] = isolated_repository
                    api_path = (
                        f"/api/session/organizations/{org}"
                        "/suppliers/supplier/precision-reviews"
                    )
                    other_original = (
                        b"sku;name;price;currency;unit;pack\n"
                        b"SKU-3;Pens;12.125;RUB;pcs;1\n"
                    )
                    other_stage = stage_supplier_prices(
                        "latest.csv", other_original,
                        SupplierMapping(
                            sku=0, name=1, price=2, currency=3,
                            sale_unit=4, pieces_per_sale_unit=5, header_row=1,
                        ),
                    )
                    audit_rationale = (
                        "Vendor terms were separately reviewed for this "
                        "particular pricing decision."
                    )
                    review_body = {
                        "source_sha256": other_stage.sha256,
                        "source_row_sha256": source_row_sha256(other_stage.rows[0]),
                        "line": 2,
                        "mapping": {
                            "sku": 0, "name": 1, "price": 2,
                            "currency": 3, "sale_unit": 4,
                            "pieces_per_sale_unit": 5, "header_row": 1,
                        },
                        "finding": "approve_rounding",
                        "rounding_mode": "half_up",
                        "rounded_price_rub": "12.13",
                        "rationale": audit_rationale,
                    }
                    headers = {
                        "Origin": "https://localhost:5173",
                        "X-CSRF-Token": csrf_token,
                        "Cookie": f"{SESSION_COOKIE}={session_token}",
                    }
                    with TestClient(api, base_url="https://testserver") as browser:
                        def submit(
                            value: dict[str, object],
                            file_bytes: bytes,
                            send_headers: dict[str, str],
                        ) -> HttpResponse:
                            result = browser.post(
                                api_path,
                                headers=send_headers,
                                files={"file": ("latest.csv", file_bytes, "text/csv")},
                                data={"review_json": json.dumps(value)},
                            )
                            assert isinstance(result, HttpResponse)
                            return result

                        mapping_path = (
                            f"/api/session/organizations/{org}"
                            "/suppliers/supplier/mapping-profiles"
                        )
                        mapping_form = {
                            "source_sha256": mapping_inspection.source_sha256,
                            "mapping": {
                                "sku": 0, "name": 1, "price": 2,
                                "currency": 3, "sale_unit": 4,
                                "header_row": 1, "delimiter": ";",
                                "encoding": "utf-8-sig",
                            },
                            "price_basis": "standard",
                            "evidence": "Operator confirmed the supplier price column",
                            "explicitly_confirmed": True,
                            "confirmed_currency": None,
                        }
                        found_mapping = browser.post(
                            mapping_path + "/match", headers=headers,
                            files={"file": ("profile.csv", mapping_original, "text/csv")},
                        )
                        assert found_mapping.status_code == 200, found_mapping.text
                        assert found_mapping.json()["found"] is True
                        assert found_mapping.json()["profile_sha256"] == (
                            mapping_profile.profile_sha256
                        )
                        drifted = browser.post(
                            mapping_path + "/match", headers=headers,
                            files={"file": (
                                "profile.csv",
                                mapping_updated.replace(b"price;", b"wholesale;"),
                                "text/csv",
                            )},
                        )
                        assert drifted.status_code == 200, drifted.text
                        assert drifted.json()["found"] is False
                        invalid_session = browser.post(
                            mapping_path,
                            headers={"Origin": headers["Origin"]},
                            files={"file": ("profile.csv", mapping_original, "text/csv")},
                            data={"approval_json": json.dumps(mapping_form)},
                        )
                        assert invalid_session.status_code == 401
                        mapped = browser.post(
                            mapping_path, headers=headers,
                            files={"file": ("profile.csv", mapping_original, "text/csv")},
                            data={"approval_json": json.dumps(mapping_form)},
                        )
                        assert mapped.status_code == 201, mapped.text
                        assert mapped.json()["profile_sha256"] == (
                            mapping_profile.profile_sha256
                        )
                        assert mapped.json()["reviewed"] is True
                        assert mapped.json()["canonical_importable"] is False
                        assert browser.post(
                            mapping_path, headers=headers,
                            files={"file": ("profile.csv", mapping_original, "text/csv")},
                            data={"approval_json": json.dumps(mapping_form)},
                        ).status_code == 201
                        changed_source = browser.post(
                            mapping_path, headers=headers,
                            files={"file": ("profile.csv", mapping_updated, "text/csv")},
                            data={"approval_json": json.dumps(mapping_form)},
                        )
                        assert changed_source.status_code == 409
                        assert changed_source.json()["code"] == "source_changed"
                        structure = browser.post(
                            api_path + "/inspect", headers=headers,
                            files={"file": ("latest.csv", other_original, "text/csv")},
                        )
                        assert structure.status_code == 200, structure.text
                        assert structure.headers["cache-control"] == "no-store"
                        assert structure.json()["source_sha256"] == other_stage.sha256
                        assert structure.json()["source_format"] == "csv"
                        assert structure.json()["stage_mapping"]["price"] == 2
                        assert structure.json()["stage_mapping"]["name"] == 1
                        assert structure.json()["canonical_importable"] is False
                        assert browser.post(
                            api_path + "/inspect", headers={
                                "Origin": headers["Origin"],
                                "Cookie": headers["Cookie"],
                            }, files={"file": ("latest.csv", other_original, "text/csv")},
                        ).status_code == 401
                        preview_form = {
                            "mapping_json": json.dumps(review_body["mapping"]),
                            "start": "0", "limit": "1",
                        }
                        preview_response = browser.post(
                            api_path + "/preview",
                            headers=headers,
                            files={"file": ("latest.csv", other_original, "text/csv")},
                            data=preview_form,
                        )
                        assert preview_response.status_code == 200, preview_response.text
                        preview_data = preview_response.json()
                        assert preview_data["source_sha256"] == other_stage.sha256
                        assert preview_data["precision_cases"] == 1
                        assert preview_data["rows"][0]["source_price_raw"] == "12.125"
                        assert preview_data["rows"][0]["source_row_sha256"] == (
                            review_body["source_row_sha256"]
                        )
                        assert preview_data["rows"][0]["currency_confirmed_rub"]
                        assert preview_data["published"] is False
                        assert preview_response.headers["cache-control"] == "no-store"
                        without_auth = {
                            "Origin": headers["Origin"],
                            "X-CSRF-Token": headers["X-CSRF-Token"],
                        }
                        assert browser.post(
                            api_path + "/preview", headers=without_auth,
                            files={"file": ("latest.csv", other_original, "text/csv")},
                            data=preview_form,
                        ).status_code == 401
                        page_source = (
                            b"sku;name;price;currency;unit;pack\n"
                            b"A1;First;10.123;RUB;pcs;1\n"
                            b"B1;Second;20.456;RUB;pcs;1\n"
                            b"C1;Third;30.789;RUB;pcs;1\n"
                        )
                        page_response = browser.post(
                            api_path + "/preview",
                            headers=headers,
                            files={"file": ("page.csv", page_source, "text/csv")},
                            data={**preview_form, "start": "1"},
                        )
                        assert page_response.status_code == 200, page_response.text
                        assert page_response.json()["precision_cases"] == 3
                        assert len(page_response.json()["rows"]) == 1
                        assert page_response.json()["rows"][0]["line"] == 3
                        assert page_response.json()["next_offset"] == 2
                        real_path = (
                            ROOT / "ml" / "data" / "real"
                            / "research_20261021_supplier_prices"
                            / "avt_el_ecoplast_20260615.xlsx"
                        )
                        if real_path.is_file():
                            blocked_formulas = browser.post(
                                api_path + "/inspect", headers=headers,
                                files={"file": (real_path.name, real_path.read_bytes(), MIME_XLSX)},
                                data={"sheet": "Прайс-лист"},
                            )
                            assert blocked_formulas.status_code == 422
                            assert blocked_formulas.json()["code"] == (
                                "xlsx_formulas_require_quarantine"
                            )
                            real_structure = browser.post(
                                api_path + "/inspect", headers=headers,
                                files={"file": (real_path.name, real_path.read_bytes(), MIME_XLSX)},
                                data={
                                    "sheet": "Прайс-лист",
                                    "formula_policy": "quarantine_rows",
                                },
                            )
                            assert real_structure.status_code == 200, real_structure.text
                            assert real_structure.json()["source_sha256"] == hashlib.sha256(
                                real_path.read_bytes()
                            ).hexdigest()
                            assert "xlsx_formula_cells_quarantined" in (
                                real_structure.json()["issues"]
                            )
                            assert real_structure.json()["requires_confirmation"] is True
                            real_response = browser.post(
                                api_path + "/preview", headers=headers,
                                files={
                                    "file": (
                                        real_path.name, real_path.read_bytes(),
                                        MIME_XLSX,
                                    ),
                                },
                                data={
                                    "mapping_json": json.dumps({
                                        "sku": 0, "name": 1, "price": 4,
                                        "currency": 5, "sale_unit": 2,
                                        "pieces_per_sale_unit": 3,
                                        "header_row": 2, "sheet": "Прайс-лист",
                                    }),
                                    "formula_policy": "quarantine_rows",
                                    "start": "0", "limit": "2",
                                },
                            )
                            assert real_response.status_code == 200, real_response.text
                            assert real_response.json()["total_rows"] == 4179
                            assert real_response.json()["precision_cases"] == 3481
                            assert len(real_response.json()["rows"]) == 2
                            assert real_response.json()["published"] is False
                        no_csrf = {key: value for key, value in headers.items() if key != "X-CSRF-Token"}
                        assert submit(review_body, other_original, no_csrf).status_code == 401
                        changed = submit(
                            review_body,
                            other_original.replace(b"12.125", b"12.126"),
                            headers,
                        )
                        assert changed.status_code == 409
                        assert changed.json()["code"] == "source_changed"
                        wrong_row = dict(review_body, source_row_sha256="f" * 64)
                        changed_row = submit(wrong_row, other_original, headers)
                        assert changed_row.status_code == 409
                        assert changed_row.json()["code"] == "source_row_changed"

                        posted = submit(review_body, other_original, headers)
                        assert posted.status_code == 201, posted.text
                        assert posted.json()["recorded"] is True
                        assert posted.json()["published"] is False
                        assert submit(review_body, other_original, headers).status_code == 201
                        response = browser.get(
                            api_path,
                            headers=headers,
                            params={"source_sha256": other_stage.sha256, "line": 2},
                        )
                        assert response.status_code == 200, response.text
                        assert len(response.json()["records"]) == 1
                        assert response.json()["records"][0]["rounded_price_rub"] == "12.13"
                        assert response.headers["cache-control"] == "no-store"
                    foreign_store = TenantSupplierPrecisionReviewStore(
                        conn, AuthenticatedUser(actor), uuid.uuid4(),
                        session_token=session_token, csrf_token=csrf_token,
                    )
                    with pytest.raises(TenantAccessDenied):
                        foreign_store.list_reviews("supplier", staged.sha256)
                    conn.execute(text("RESET ROLE"))
                    with pytest.raises(IntegrityError), conn.begin_nested():
                        conn.execute(text("""
                            INSERT INTO public.tenant_supplier_precision_reviews(
                                organization_id,supplier_id,source_sha256,source_line,
                                source_row_sha256,source_price_raw,source_price_exact,
                                finding,rounding_mode,rounded_price_rub,
                                rationale,reviewer_user_id
                            ) VALUES(
                                :org,'supplier',:source_hash,2,:row_hash,'10.125',
                                10.125,'approve_rounding','half_up',10.12,
                                'The wrong rounded amount must fail constraint',:actor
                            )
                        """), {
                            "org": org, "source_hash": "a" * 64,
                            "row_hash": "b" * 64, "actor": actor,
                        })
                    # Three separately attested source rows are immutable,
                    # including the record created through the HTTPS API.
                    assert conn.scalar(text(
                        "SELECT count(*) FROM public.tenant_supplier_precision_reviews"
                    )) == 3
                    with pytest.raises(DBAPIError), conn.begin_nested():
                        conn.execute(text("""
                            UPDATE public.tenant_supplier_precision_reviews
                            SET rounded_price_rub = 10.12 WHERE organization_id=:org
                        """), {"org": org})
                    assert conn.scalar(text(
                        "SELECT count(*) FROM public.tenant_supplier_precision_reviews"
                    )) == 3
                finally:
                    transaction.rollback()
        finally:
            clone.dispose()
    finally:
        try:
            if created:
                drop_test_clone(admin, name)
        finally:
            admin.dispose()
