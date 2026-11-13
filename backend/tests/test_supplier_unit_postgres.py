"""A31 real PostgreSQL tenant unit/pack audit on disposable marked clone only."""
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
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from postgres_clone_connection import drop_legacy_test_clone as drop_test_clone
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError

from kpdesk.api.tenant_session_routes import SESSION_COOKIE, get_unreleased_auth_repository
from kpdesk.catalog.supplier_canonical_preview import source_row_sha256
from kpdesk.catalog.supplier_precision_storage import TenantSupplierPrecisionReviewStore
from kpdesk.catalog.supplier_staging import SupplierMapping, stage_supplier_prices
from kpdesk.catalog.supplier_unit_storage import TenantSupplierUnitReviewStore
from kpdesk.db.auth_sessions import AuthSessionRepository
from kpdesk.db.tenant_access import AuthenticatedUser, TenantAccessDenied
from kpdesk.main import create_app

ROOT = Path(__file__).resolve().parents[2]


def _engine(name: str, *, autocommit: bool = False) -> Engine:
    credentials = dotenv_values(ROOT / ".env")
    password = credentials.get("KP_DESK_DB_PASSWORD")
    assert isinstance(password, str) and len(password) >= 32
    return create_engine(URL.create(
        "postgresql+psycopg", username="kpdesk", password=password,
        host="127.0.0.1", port=int(credentials.get("KP_DESK_DB_PORT") or 55432),
        database=name,
    ), isolation_level="AUTOCOMMIT" if autocommit else None)


@pytest.mark.skipif(
    os.environ.get("KP_DESK_RUN_ISOLATED_UNIT_PG") != "1",
    reason="Explicit opt-in: marked disposable PostgreSQL clone from working release",
)
def test_a31_unit_review_real_postgres_restrictions_and_idempotency() -> None:
    nonce = secrets.token_hex(6)
    name = f"kpdesk_accounts_http_{nonce}"
    assert re.fullmatch(r"kpdesk_accounts_http_[0-9a-f]{12}", name)
    admin = _engine("postgres", autocommit=True)
    created = False
    try:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'CREATE DATABASE "{name}" TEMPLATE template0 OWNER kpdesk')
            created = True
            conn.exec_driver_sql(
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
                stdin=backup.stdout, capture_output=True, check=False, timeout=150,
            )
            backup.stdout.close()
            assert backup.wait(timeout=150) == 0
            assert restored.returncode == 0, restored.stderr.decode(
                "utf-8", errors="replace",
            )[-600:]
        engine = _engine(name)
        try:
            with engine.connect() as conn:
                trans = conn.begin()
                try:
                    source_revision = conn.scalar(
                        text("SELECT version_num FROM alembic_version")
                    )
                    assert source_revision in (
                        "0023_supplier_mapping_history", "0024_supplier_unit_reviews", "0025_request_candidate_audit", "0026_request_source_restrictions", "0027_candidate_adjudications", "0028_quote_manager_decisions", "0029_manager_decisions_read", "0030_tenant_llm_cache", "0031_catalog_taxonomy_v4",
                    )
                    if source_revision == "0023_supplier_mapping_history":
                        migration = importlib.import_module(
                            "migrations.versions.0024_supplier_unit_reviews"
                        )
                        with Operations.context(MigrationContext.configure(conn)):
                            migration.upgrade()
                    assert conn.execute(text("""
                        SELECT relrowsecurity,relforcerowsecurity FROM pg_class
                        WHERE oid=to_regclass('public.tenant_supplier_unit_reviews')
                    """)).one() == (True, True)
                    assert conn.scalar(text("""
                        SELECT has_table_privilege(
                           'kpdesk_tenant_runtime',
                           'public.tenant_supplier_unit_reviews','INSERT'
                        )
                    """)) is False
                    assert conn.scalar(text("""
                        SELECT has_function_privilege(
                           'kpdesk_tenant_runtime',
                           'public.kpdesk_supplier_unit_attest(uuid,text,text,integer,text,text,integer,text,text,integer,text)',
                           'EXECUTE'
                        )
                    """)) is True
                    org, outsider, reviewer = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
                    conn.execute(text("""
                        INSERT INTO organizations(id,name,status)
                        VALUES(:org,'Unit review test','active'),
                              (:outsider,'Unit outsider','active')
                    """), {"org": org, "outsider": outsider})
                    conn.execute(text("""
                        INSERT INTO users(id,email,status,email_verified_at)
                        VALUES(:reviewer,:email,'active',clock_timestamp())
                    """), {"reviewer": reviewer, "email": f"unit-{nonce}@invalid.local"})
                    conn.execute(text("""
                        INSERT INTO organization_memberships(
                          organization_id,user_id,role,status
                        ) VALUES(:org,:reviewer,'editor','active')
                    """), {"org": org, "reviewer": reviewer})
                    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                    conn.execute(text("""
                        INSERT INTO auth_sessions(
                            token_sha256,user_id,csrf_sha256,expires_at
                        ) VALUES(:digest,:reviewer,:csrf,clock_timestamp()+interval '1 hour')
                    """), {
                        "digest": hashlib.sha256(token.encode("ascii")).hexdigest(),
                        "reviewer": reviewer,
                        "csrf": hashlib.sha256(csrf.encode("ascii")).hexdigest(),
                    })
                    content = (
                        b"sku;name;price;currency;unit;pack\n"
                        b"P-01;Office item;250.00;RUB;pcs;\n"
                    )
                    staged = stage_supplier_prices("vendor.csv", content, SupplierMapping(
                        name=1, sku=0, price=2, currency=3, sale_unit=4,
                        pieces_per_sale_unit=5, header_row=1,
                    ))
                    row = staged.rows[0]
                    identity = AuthenticatedUser(reviewer)
                    conn.execute(text("SET ROLE kpdesk_tenant_runtime"))
                    scope = TenantSupplierPrecisionReviewStore(
                        conn, identity, org, token, csrf,
                    )
                    store = TenantSupplierUnitReviewStore(scope)
                    rationale = (
                        "Operator checked supplier source tariff: amount is per "
                        "single original sale piece, despite missing pack quantity."
                    )
                    assert store.attest(
                        staged, "vendor", row, finding="confirm_price_unit",
                        approved_sale_unit="штука", approved_pack_size=1,
                        rationale=rationale,
                    )
                    assert store.attest(
                        staged, "vendor", row, finding="confirm_price_unit",
                        approved_sale_unit="штука", approved_pack_size=1,
                        rationale=rationale,
                    )
                    assert len(store.list_reviews("vendor", staged.sha256)) == 1
                    # Exercise the actual HTTPS tenant entrypoint on this same
                    # rollback-only DB transaction and authenticated session.
                    def isolated_repository() -> Iterator[AuthSessionRepository]:
                        yield AuthSessionRepository(conn)

                    app = create_app(testing=True, tenant_sessions=True)
                    app.dependency_overrides[
                        get_unreleased_auth_repository
                    ] = isolated_repository
                    headers = {
                        "Origin": "https://localhost:5173",
                        "X-CSRF-Token": csrf,
                        "Cookie": f"{SESSION_COOKIE}={token}",
                    }
                    source_mapping = {
                        "sku": 0, "name": 1, "price": 2, "currency": 3,
                        "sale_unit": 4, "pieces_per_sale_unit": 5, "header_row": 1,
                        "source_sha256": staged.sha256,
                    }
                    body = {
                        "source_sha256": staged.sha256,
                        "source_row_sha256": source_row_sha256(row),
                        "line": row.line,
                        "mapping": source_mapping,
                        "finding": "confirm_price_unit",
                        "approved_sale_unit": "штука",
                        "approved_pack_size": 1,
                        "rationale": rationale,
                        "personally_reviewed": True,
                    }
                    endpoint = (
                        f"/api/session/organizations/{org}"
                        "/suppliers/vendor-http/unit-reviews"
                    )
                    with TestClient(app, base_url="https://testserver") as browser:
                        stage_reply = browser.post(
                            f"/api/session/organizations/{org}"
                            "/suppliers/vendor-http/precision-reviews/stage",
                            headers=headers, files={"file": ("vendor.csv", content)},
                            data={"mapping_json": json.dumps(source_mapping)},
                        )
                        assert stage_reply.status_code == 200, stage_reply.text
                        assert stage_reply.json()["sampled_rows"][0][
                            "source_row_sha256"
                        ] == source_row_sha256(row)
                        saved = browser.post(
                            endpoint, headers=headers,
                            files={"file": ("vendor.csv", content)},
                            data={"review_json": json.dumps(body, ensure_ascii=False)},
                        )
                        assert saved.status_code == 201, saved.text
                        assert saved.json()["recorded"] is True
                        assert saved.json()["published"] is False
                        history = browser.get(
                            endpoint, params={"source_sha256": staged.sha256},
                            headers=headers,
                        )
                        assert history.status_code == 200, history.text
                        assert len(history.json()["records"]) == 1
                        assert history.json()["records"][0]["approved_pack_size"] == 1
                        assert history.json()["model_training_authorized"] is False
                        foreign = browser.get(
                            f"/api/session/organizations/{outsider}"
                            "/suppliers/vendor-http/unit-reviews",
                            params={"source_sha256": staged.sha256},
                            headers=headers,
                        )
                        assert foreign.status_code == 403
                    with pytest.raises(TenantAccessDenied):
                        store.attest(
                            staged, "vendor", row, finding="confirm_price_unit",
                            approved_sale_unit="пачка", approved_pack_size=12,
                            rationale="A conflicting reviewed pack must not overwrite audit.",
                        )
                    with pytest.raises(ValueError, match="Unresolved"):
                        store.attest(
                            staged, "vendor", row, finding="reject_source_unit",
                            approved_sale_unit="пачка", approved_pack_size=12,
                            rationale=rationale,
                        )
                    assert conn.scalar(text("""
                        SELECT public.kpdesk_supplier_unit_attest(
                          :org,'vendor',:source_sha,3,:row_sha,NULL,NULL,
                          'confirm_price_unit','штука',0,:rationale
                        )
                    """), {
                        "org": org, "source_sha": staged.sha256, "row_sha": "a"*64,
                        "rationale": rationale,
                    }) is False
                    outsider_store = TenantSupplierUnitReviewStore(
                        TenantSupplierPrecisionReviewStore(
                            conn, identity, outsider, token, csrf,
                        )
                    )
                    with pytest.raises(TenantAccessDenied):
                        outsider_store.list_reviews("vendor", staged.sha256)
                    with pytest.raises(DBAPIError), conn.begin_nested():
                        conn.execute(text("""
                            INSERT INTO public.tenant_supplier_unit_reviews(
                                organization_id,supplier_id,source_sha256,source_line,
                                source_row_sha256,finding,rationale,reviewer_user_id
                            ) VALUES(:org,'vendor',:source_sha,4,:row_sha,
                                'reject_source_unit',:rationale,:reviewer)
                        """), {
                            "org": org, "source_sha": staged.sha256, "row_sha": "b"*64,
                            "rationale": rationale, "reviewer": reviewer,
                        })
                    conn.execute(text("RESET ROLE"))
                    with pytest.raises(DBAPIError), conn.begin_nested():
                        conn.execute(text("""
                            UPDATE public.tenant_supplier_unit_reviews
                            SET rationale='Attempt to rewrite authenticated audit'
                            WHERE organization_id=:org
                        """), {"org": org})
                finally:
                    trans.rollback()
        finally:
            engine.dispose()
    finally:
        try:
            if created:
                drop_test_clone(admin, name)
        finally:
            admin.dispose()
