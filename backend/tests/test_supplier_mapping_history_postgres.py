"""A26 history integration on a disposable, tagged PostgreSQL release clone."""
from __future__ import annotations

import csv
import hashlib
import importlib
import io
import json
import os
import re
import secrets
import subprocess
import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from fastapi.testclient import TestClient
from postgres_clone_connection import drop_legacy_test_clone as drop_test_clone
from postgres_clone_connection import postgres_test_engine as _engine
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from kpdesk.api.tenant_session_routes import SESSION_COOKIE, get_unreleased_auth_repository
from kpdesk.catalog.parse import parse_price_list
from kpdesk.catalog.schema import COLUMNS_V2, ImportLimits
from kpdesk.catalog.supplier_inference import inspect_supplier_source
from kpdesk.catalog.supplier_mapping_storage import TenantSupplierMappingStore
from kpdesk.catalog.supplier_profiles import approve_supplier_profile
from kpdesk.catalog.tenant_storage import TenantCatalogRepository
from kpdesk.catalog.verified_supplier_sources import SupplierSourceRef
from kpdesk.db.auth_sessions import AuthSessionRepository
from kpdesk.db.tenant_access import AuthenticatedUser, TenantAccessDenied
from kpdesk.extraction.schema import ExtractedRequest
from kpdesk.main import create_app
from kpdesk.quotes.tenant_storage import TenantQuoteRepository

SOURCE = b"sku;name;price;currency\nS1;Cable;12.50;RUB\n"


def _canonical_catalog(price: str) -> bytes:
    content = io.StringIO(newline="")
    writer = csv.writer(content, delimiter=";", lineterminator="\n")
    writer.writerow(COLUMNS_V2)
    row: dict[str, str | int] = {
        "sku": "PENCIL-SAME", "name": "Graphite pencil HB",
        "category": "pencil", "sale_unit": "штука",
        "pieces_per_sale_unit": 1, "price_rub": price,
        "brand": "PaperCo", "attributes_json": json.dumps({
            "pencil_type": "graphite", "lead_grade": "HB",
        }),
    }
    writer.writerow([row.get(key, "") for key in COLUMNS_V2])
    return content.getvalue().encode("utf-8")




@pytest.mark.skipif(
    os.environ.get("KP_DESK_RUN_ISOLATED_MAPPING_PG") != "1",
    reason="Explicit opt-in to create/remove a tagged PostgreSQL snapshot clone",
)
def test_a26_history_is_session_bound_tenant_scoped_and_paginated() -> None:
    admin = _engine("postgres", autocommit=True)
    nonce = secrets.token_hex(6)
    name = f"kpdesk_accounts_http_{nonce}"
    assert re.fullmatch(r"kpdesk_accounts_http_[0-9a-f]{12}", name)
    created = False
    try:
        source = _engine("kpdesk")
        try:
            with source.connect() as connection:
                assert connection.scalar(text("SELECT count(*) FROM users")) == 0
                assert connection.scalar(text("SELECT count(*) FROM organizations")) == 0
        finally:
            source.dispose()
        with admin.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{name}" TEMPLATE template0 OWNER kpdesk')
            created = True
            connection.exec_driver_sql(
                f"COMMENT ON DATABASE \"{name}\" IS 'kpdesk:accounts-http:{nonce}'"
            )
        # Stream a consistent snapshot instead of TEMPLATE kpdesk: unrelated
        # test connections must not block creation of this disposable clone.
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
                    revision = conn.scalar(text("SELECT version_num FROM alembic_version"))
                    assert revision in (
                        "0022_supplier_mapping_profiles", "0023_supplier_mapping_history",
                        "0024_supplier_unit_reviews", "0025_request_candidate_audit", "0026_request_source_restrictions", "0027_candidate_adjudications", "0028_quote_manager_decisions", "0029_manager_decisions_read", "0030_tenant_llm_cache", "0031_catalog_taxonomy_v4",
                    )
                    assert revision == "0031_catalog_taxonomy_v4"
                    with Operations.context(MigrationContext.configure(conn)):
                        importlib.import_module(
                            "migrations.versions.0032_supplier_quote_audit"
                        ).upgrade()
                    assert conn.scalar(text("""
                        SELECT has_function_privilege(
                          'kpdesk_tenant_runtime',
                          'public.kpdesk_supplier_mapping_history(uuid,text,integer,integer)',
                          'EXECUTE'
                        )
                    """)) is True
                    assert conn.scalar(text("""
                        SELECT has_table_privilege(
                          'kpdesk_tenant_runtime',
                          'public.tenant_supplier_mapping_profiles','SELECT'
                        )
                    """)) is False
                    assert conn.execute(text("""
                        SELECT relrowsecurity,relforcerowsecurity
                        FROM pg_class
                        WHERE oid=to_regclass('public.tenant_supplier_mapping_profiles')
                    """)).one() == (True, True)
                    org, outsider, actor = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
                    conn.execute(text("""
                        INSERT INTO organizations(id,name,status)
                        VALUES (:org,'History tests','active'),
                               (:outsider,'Different org','active')
                    """), {"org": org, "outsider": outsider})
                    conn.execute(text("""
                        INSERT INTO users(id,email,status,email_verified_at)
                        VALUES(:actor,:email,'active',clock_timestamp())
                    """), {"actor": actor, "email": f"history-{nonce}@invalid.local"})
                    conn.execute(text("""
                        INSERT INTO organization_memberships(
                          organization_id,user_id,role,status
                        ) VALUES(:org,:actor,'owner','active')
                    """), {"org": org, "actor": actor})
                    token = secrets.token_urlsafe(32)
                    csrf = secrets.token_urlsafe(32)
                    conn.execute(text("""
                        INSERT INTO auth_sessions(
                          token_sha256,user_id,csrf_sha256,expires_at
                        ) VALUES(:digest,:actor,:csrf,clock_timestamp()+interval '1 hour')
                    """), {
                        "digest": hashlib.sha256(token.encode("ascii")).hexdigest(),
                        "csrf": hashlib.sha256(csrf.encode("ascii")).hexdigest(),
                        "actor": actor,
                    })
                    conn.execute(text("SET ROLE kpdesk_tenant_runtime"))
                    identity = AuthenticatedUser(actor)
                    store = TenantSupplierMappingStore(conn, identity, org, token, csrf)
                    inspected = inspect_supplier_source("prices.csv", SOURCE)
                    fields = dict(inspected.proposed_fields)
                    first = approve_supplier_profile(
                        inspected, organization_id=org, supplier_id="supplier",
                        reviewer=identity, source_sha256=inspected.source_sha256,
                        selected_columns=fields, price_basis="standard",
                        confirmed_currency=None,
                        review_evidence="Operator checked original columns",
                        explicitly_confirmed=True,
                    )
                    second = approve_supplier_profile(
                        inspected, organization_id=org, supplier_id="supplier",
                        reviewer=identity, source_sha256=inspected.source_sha256,
                        selected_columns={key: value for key, value in fields.items()
                                          if key != "sku"}, price_basis="standard",
                        confirmed_currency=None,
                        review_evidence="Second independent check of name and price",
                        explicitly_confirmed=True,
                    )
                    assert first.profile_sha256 != second.profile_sha256
                    store.save(first)
                    assert store.latest("supplier", inspected) == first
                    store.save(second)
                    store.save(second)
                    # Distinct approved column decisions are not a safe latest map.
                    assert store.latest("supplier", inspected) is None
                    consistent_profiles = [
                        approve_supplier_profile(
                            inspected, organization_id=org, supplier_id="consistent",
                            reviewer=identity, source_sha256=inspected.source_sha256,
                            selected_columns=fields, price_basis="standard",
                            confirmed_currency=None, review_evidence=evidence,
                            explicitly_confirmed=True,
                        )
                        for evidence in (
                            "First verified check of original headers",
                            "Another documented check of original headers",
                        )
                    ]
                    for reviewed in consistent_profiles:
                        store.save(reviewed)
                    matching = store.latest("consistent", inspected)
                    assert matching is not None
                    assert matching.profile_sha256 in {
                        reviewed.profile_sha256 for reviewed in consistent_profiles
                    }
                    entries = store.history("supplier")
                    assert {item.profile.profile_sha256 for item in entries} == {
                        first.profile_sha256, second.profile_sha256,
                    }
                    assert len(store.history("supplier", limit=1, offset=0)) == 1
                    assert len(store.history("supplier", limit=1, offset=1)) == 1
                    assert store.history("not-the-supplier") == []
                    with pytest.raises(ValueError, match="pagination"):
                        store.history("supplier", limit=51)
                    with pytest.raises(DBAPIError), conn.begin_nested():
                        conn.execute(text(
                            "SELECT count(*) FROM public.tenant_supplier_mapping_profiles"
                        ))
                    with pytest.raises(TenantAccessDenied):
                        TenantSupplierMappingStore(
                            conn, identity, outsider, token, csrf,
                        ).history("supplier")
                    assert store.history("supplier")  # Rebinding clears denied tenant context.

                    # Real PostgreSQL: two supplier-local identical SKU values,
                    # distinct source bytes, approved profiles and immutable versions.
                    published_sources: list[SupplierSourceRef] = []
                    published_version_ids: list[uuid.UUID] = []
                    tenant_catalog = TenantCatalogRepository(
                        conn, identity, org, session_token=token, csrf_token=csrf,
                    )
                    for supplier_name, price in (
                        ("supplier_a", "29.90"), ("supplier_b", "19.90"),
                    ):
                        raw = _canonical_catalog(price)
                        parsed = parse_price_list(
                            supplier_name + ".csv", raw, ImportLimits(),
                        )
                        assert parsed.publishable
                        version = tenant_catalog.publish(
                            parsed, supplier_name + "-catalog", parsed.source_sha256,
                        )
                        inspection = inspect_supplier_source(
                            supplier_name + ".csv", raw,
                        )
                        profile = approve_supplier_profile(
                            inspection,
                            organization_id=org, supplier_id=supplier_name,
                            reviewer=identity, source_sha256=inspection.source_sha256,
                            selected_columns={"sku": 0, "name": 1, "price": 5},
                            price_basis="standard", confirmed_currency="RUB",
                            review_evidence="Checked immutable original supplier CSV",
                            explicitly_confirmed=True,
                        )
                        store.save(profile)
                        published_sources.append(SupplierSourceRef(
                            supplier_name, profile.fingerprint, version.id,
                        ))
                        published_version_ids.append(version.id)
                    request_data = ExtractedRequest.model_validate({"lines": [{
                        "source_quote": "Graphite pencil HB",
                        "description": "Graphite pencil HB",
                        "category": "pencil", "quantity": 2, "unit": "штука",
                        "brand": None, "paper_format": None, "paper_gsm": None,
                        "pen_ink_color": None, "pen_ink_type": None,
                        "attributes": {"pencil_type": "graphite", "lead_grade": "HB"},
                    }]})
                    quotes = TenantQuoteRepository(
                        conn, identity, org, token, csrf,
                    )
                    compared = quotes.compare_suppliers(
                        tuple(published_sources), request_data, None,
                    )
                    assert [offer.supplier_id for offer in compared.lines[0].offers] == [
                        "supplier_b", "supplier_a",
                    ]
                    assert [offer.base_total_rub for offer in compared.lines[0].offers] == [
                        Decimal("39.80"), Decimal("59.80"),
                    ]
                    assert {entry.id for entry in tenant_catalog.versions()} >= set(
                        published_version_ids
                    )

                    def repository() -> Iterator[AuthSessionRepository]:
                        yield AuthSessionRepository(conn)

                    app = create_app(testing=True, tenant_sessions=True)
                    app.dependency_overrides[get_unreleased_auth_repository] = repository
                    with TestClient(app, base_url="https://testserver") as client:
                        url = (f"/api/session/organizations/{org}/suppliers/"
                               "supplier/mapping-profiles/history")
                        headers = {"Cookie": f"{SESSION_COOKIE}={token}"}
                        match = client.post(
                            url.removesuffix("/history") + "/match",
                            headers={**headers, "X-CSRF-Token": csrf,
                                     "Origin": "https://localhost:5173"},
                            files={"file": ("prices.csv", SOURCE, "text/csv")},
                        )
                        assert match.status_code == 200, match.text
                        assert match.json()["found"] is False
                        assert match.json()["model_training_authorized"] is False
                        result = client.get(url, headers=headers, params={"limit": 1})
                        assert result.status_code == 200, result.text
                        assert len(result.json()["items"]) == 1
                        assert result.json()["next_offset"] == 1
                        assert result.json()["canonical_importable"] is False
                        assert result.json()["model_training_authorized"] is False
                        assert result.headers["cache-control"] == "no-store"
                        next_page = client.get(
                            url, headers=headers, params={"limit": 1, "offset": 1},
                        )
                        assert next_page.status_code == 200
                        assert next_page.json()["next_offset"] is None
                        assert client.get(url).status_code == 401
                        catalog_history = client.get(
                            f"/api/session/organizations/{org}/catalog/versions",
                            headers=headers,
                        )
                        assert catalog_history.status_code == 200, catalog_history.text
                        assert {
                            row["version_id"] for row in catalog_history.json()["items"]
                        } >= {str(version) for version in published_version_ids}
                        base = f"/api/session/organizations/{org}/quotes/supplier-comparison"
                        body = {
                            "sources": [{
                                "supplier_id": item.supplier_id,
                                "profile_fingerprint": item.profile_fingerprint,
                                "catalog_version_id": str(item.catalog_version_id),
                            } for item in published_sources],
                            "extracted_request": request_data.model_dump(mode="json"),
                        }
                        writes = {
                            **headers,
                            "Origin": "https://localhost:5173",
                            "X-CSRF-Token": csrf,
                        }
                        preview_result = client.post(
                            base + "/preview", json=body, headers=writes,
                        )
                        assert preview_result.status_code == 200, preview_result.text
                        offers = preview_result.json()["lines"][0]["offers"]
                        assert [offer["supplier_id"] for offer in offers] == [
                            "supplier_b", "supplier_a",
                        ]
                        assert [offer["selling_total_rub"] for offer in offers] == [
                            "39.80", "59.80",
                        ]
                        selected = {
                            **body,
                            "active_catalog_version_id": str(published_version_ids[-1]),
                            "selections": [{
                                "line_number": 1, "supplier_id": "supplier_a",
                                "catalog_version_id": str(published_version_ids[0]),
                                "sku": "PENCIL-SAME",
                            }],
                        }
                        created_quote = client.post(
                            base, json=selected,
                            headers={**writes, "Idempotency-Key": "multi-supplier-quote"},
                        )
                        assert created_quote.status_code == 201, created_quote.text
                        saved = created_quote.json()
                        assert saved["ready_for_draft"] is True
                        assert saved["total_rub"] == "59.80"
                        assert saved["lines"][0]["supplier_id"] == "supplier_a"
                        assert saved["lines"][0]["source_catalog_version_id"] == (
                            str(published_version_ids[0])
                        )
                        assert saved["lines"][0]["source_sha256"] == (
                            hashlib.sha256(_canonical_catalog("29.90")).hexdigest()
                        )
                        repeated = client.post(
                            base, json=selected,
                            headers={**writes, "Idempotency-Key": "multi-supplier-quote"},
                        )
                        assert repeated.status_code == 201
                        assert repeated.json()["quote_id"] == saved["quote_id"]
                        stored = client.get(
                            f"/api/session/organizations/{org}/quotes/{saved['quote_id']}",
                            headers=headers,
                        )
                        assert stored.status_code == 200
                        assert stored.json()["snapshot_sha256"] == saved["snapshot_sha256"]
                        audit = client.get(
                            f"/api/session/organizations/{org}/quotes/"
                            f"{saved['quote_id']}/manager-decisions/1",
                            headers=headers,
                        )
                        assert audit.status_code == 200, audit.text
                        assert audit.json()["items"][0]["catalog_version_id"] == (
                            str(published_version_ids[0])
                        )
                        different_org = base.replace(str(org), str(outsider))
                        assert client.post(
                            different_org + "/preview", json=body, headers=writes,
                        ).status_code == 403
                        assert client.get(url, headers=headers, params={"limit": 50}).status_code == 422
                        foreign = url.replace(str(org), str(outsider))
                        assert client.get(foreign, headers=headers).status_code == 403
                    conn.execute(text("""
                        SELECT set_config('kpdesk.session_sha256','',true)
                    """))
                    assert conn.scalar(text("""
                        SELECT count(*) FROM public.kpdesk_supplier_mapping_history(
                            :org,'supplier',20,0
                        )
                    """), {"org": org}) == 0
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
