"""A9 tenant-scoped quote history against real A8 PostgreSQL RLS.

A3-A8 schema, test grants, synthetic users/catalogs/quotes are created and
rolled back atomically on kpdesk_tenant_test. Production remains unchanged.
"""
from __future__ import annotations

import csv
import importlib
import io
import json
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError

from kpdesk.catalog.parse import parse_price_list
from kpdesk.catalog.schema import COLUMNS_V2, ImportLimits
from kpdesk.catalog.tenant_storage import TenantCatalogRepository
from kpdesk.db.auth_sessions import AuthSessionRepository, hash_password
from kpdesk.db.tenant_access import AuthenticatedUser, TenantAccessDenied
from kpdesk.extraction.schema import ExtractedRequest
from kpdesk.quotes.instructions import ConfirmedInstructions, propose_instructions
from kpdesk.quotes.storage import ManagerSkuSelection, QuoteConflict, QuoteNotFound
from kpdesk.quotes.tenant_storage import TenantQuoteRepository

ROOT = Path(__file__).resolve().parents[2]


def _catalog(price: str) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=";", lineterminator="\n")
    writer.writerow(COLUMNS_V2)
    product: dict[str, str | int] = {
        "sku": "PENCIL-1", "name": "Graphite pencil HB", "category": "pencil",
        "sale_unit": "\u0448\u0442\u0443\u043a\u0430",
        "pieces_per_sale_unit": 1, "price_rub": price,
        "brand": "Test", "attributes_json": json.dumps({
            "pencil_type": "graphite", "lead_grade": "HB",
        }),
    }
    writer.writerow([product.get(key, "") for key in COLUMNS_V2])
    return stream.getvalue().encode("utf-8")


def _request(quantity: int | None = 2) -> ExtractedRequest:
    return ExtractedRequest.model_validate({"lines": [{
        "source_quote": "Graphite HB pencils",
        "description": "Graphite pencil HB", "category": "pencil",
        "quantity": quantity, "unit": "\u0448\u0442\u0443\u043a\u0430",
        "brand": None, "paper_format": None, "paper_gsm": None,
        "pen_ink_color": None, "pen_ink_type": None,
        "attributes": {"pencil_type": "graphite", "lead_grade": "HB"},
    }]})


def test_two_tenant_quote_history_idempotency_and_forced_rls() -> None:
    env = dotenv_values(ROOT / ".env")
    secret = env.get("KP_DESK_DB_PASSWORD")
    assert isinstance(secret, str) and len(secret) >= 32
    engine = create_engine(URL.create(
        "postgresql+psycopg", username="kpdesk", password=secret,
        host="127.0.0.1", port=int(env.get("KP_DESK_DB_PORT") or 55432),
        database="kpdesk_tenant_test",
    ))
    org_a, org_b, person_a, person_b, viewer = (uuid.uuid4() for _ in range(5))
    quote_a_id: uuid.UUID | None = None
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                    "0006_legacy_quarantine"
                )
                with Operations.context(MigrationContext.configure(connection)):
                    for version in (
                        "0007_tenant_contract_rls", "0008_supplier_review_rls",
                        "0009_auth_sessions", "0010_auth_gateway",
                        "0011_auth_role_split", "0012_session_bound_rls",
                    ):
                        importlib.import_module("migrations.versions." + version).upgrade()
                for org, label in ((org_a, "Quote Org A"), (org_b, "Quote Org B")):
                    connection.execute(text("""
                        INSERT INTO organizations(id,name,status)
                        VALUES(:id,:name,'active')
                    """), {"id": org, "name": label})
                for person, email in (
                    (person_a, "a@tenantquotes.invalid"),
                    (person_b, "b@tenantquotes.invalid"),
                    (viewer, "viewer@tenantquotes.invalid"),
                ):
                    connection.execute(text("""
                        INSERT INTO users(id,email,password_hash,status,email_verified_at)
                        VALUES (:id,:email,:hash,'active',now())
                    """), {
                        "id": person, "email": email,
                        "hash": hash_password("TenantQuote-Verified-Password-2026"),
                    })
                for org, person, role in (
                    (org_a, person_a, "owner"), (org_b, person_b, "owner"),
                    (org_a, viewer, "viewer"),
                ):
                    connection.execute(text("""
                        INSERT INTO organization_memberships(
                            organization_id,user_id,role,status
                        ) VALUES (:org,:user,:role,'active')
                    """), {"org": org, "user": person, "role": role})
                connection.execute(text("GRANT USAGE ON SCHEMA public TO kpdesk_app"))
                for table in (
                    "catalog_versions", "catalog_items", "catalog_active",
                    "idempotency_records", "quote_documents", "quote_revisions",
                    "quote_write_requests",
                ):
                    connection.exec_driver_sql(
                        f"GRANT SELECT, INSERT ON TABLE public.{table} TO kpdesk_app"
                    )
                connection.execute(text("""
                    GRANT UPDATE ON TABLE catalog_active, quote_documents TO kpdesk_app
                """))
                connection.execute(text("SET ROLE kpdesk_auth_test"))
                token_a = AuthSessionRepository(connection).login(
                    "a@tenantquotes.invalid", "TenantQuote-Verified-Password-2026",
                    peer="192.0.2.90",
                )
                token_b = AuthSessionRepository(connection).login(
                    "b@tenantquotes.invalid", "TenantQuote-Verified-Password-2026",
                    peer="192.0.2.90",
                )
                viewer_token = AuthSessionRepository(connection).login(
                    "viewer@tenantquotes.invalid",
                    "TenantQuote-Verified-Password-2026",
                    peer="192.0.2.90",
                )
                connection.execute(text("SET ROLE kpdesk_app"))
                catalog_a = TenantCatalogRepository(
                    connection, AuthenticatedUser(person_a), org_a,
                    session_token=token_a.session_token, csrf_token=token_a.csrf_token,
                )
                catalog_b = TenantCatalogRepository(
                    connection, AuthenticatedUser(person_b), org_b,
                    session_token=token_b.session_token, csrf_token=token_b.csrf_token,
                )
                parsed_a = parse_price_list(
                    "a.csv", _catalog("19.90"), ImportLimits(),
                )
                parsed_b = parse_price_list(
                    "b.csv", _catalog("39.90"), ImportLimits(),
                )
                assert parsed_a.publishable and parsed_b.publishable
                version_a = catalog_a.publish(
                    parsed_a, "same-catalog-key", parsed_a.source_sha256,
                ).id
                version_b = catalog_b.publish(
                    parsed_b, "same-catalog-key", parsed_b.source_sha256,
                ).id
                quote_a = TenantQuoteRepository(
                    connection, AuthenticatedUser(person_a), org_a,
                    session_token=token_a.session_token, csrf_token=token_a.csrf_token,
                )
                quote_b = TenantQuoteRepository(
                    connection, AuthenticatedUser(person_b), org_b,
                    session_token=token_b.session_token, csrf_token=token_b.csrf_token,
                )
                viewer_a = TenantQuoteRepository(
                    connection, AuthenticatedUser(viewer), org_a,
                    session_token=viewer_token.session_token,
                    csrf_token=viewer_token.csrf_token,
                )

                first = quote_a.create(version_a, _request(2), "same-quote-key")
                quote_a_id = first.quote_id
                assert first.revision == 1
                assert first.total_rub == Decimal("39.80")
                assert first.lines[0].unit_price_rub == Decimal("19.90")
                assert not first.approved
                assert quote_a.create(version_a, _request(2), "same-quote-key") == first
                assert quote_a.get(first.quote_id) == first
                selected_a = quote_a.create(
                    version_a, _request(2), "selected-a",
                    [ManagerSkuSelection(line_number=1, sku="PENCIL-1")],
                )
                assert selected_a.lines[0].status == "manager_selected"
                assert selected_a.lines[0].unit_price_rub == Decimal("19.90")
                assert selected_a.total_rub == Decimal("39.80")
                assert viewer_a.get(first.quote_id) == first

                rules_text = "Наценка 18%. Бюджет до 40 рублей."
                rules_preview = propose_instructions(rules_text)
                assert rules_preview.can_confirm
                confirmed_rules = ConfirmedInstructions(
                    raw_text=rules_text, rules=rules_preview.rules,
                )
                blocked = quote_a.create(
                    version_a, _request(2), "rules-budget",
                    instructions=confirmed_rules,
                )
                assert not blocked.ready_for_draft
                assert blocked.total_rub is None
                assert blocked.estimated_total_rub == Decimal("46.96")
                assert blocked.base_total_rub == Decimal("39.80")
                assert blocked.budget_exceeded
                assert blocked.instructions == confirmed_rules
                assert quote_a.get(blocked.quote_id) == blocked
                assert connection.scalar(text("""
                    SELECT (snapshot->>'estimated_total_rub')::numeric
                    FROM quote_revisions
                    WHERE organization_id=:org AND quote_id=:quote
                      AND is_complete = false AND total_rub IS NULL
                """), {"org": org_a, "quote": blocked.quote_id}) == Decimal("46.96")
                assert quote_a.create(
                    version_a, _request(2), "rules-budget", instructions=confirmed_rules,
                ) == blocked
                with pytest.raises(QuoteConflict, match="idempotency_conflict"):
                    quote_a.create(version_a, _request(2), "rules-budget")

                brand_text = "Только бренд Test. Наценка 10%."
                brand_rules = propose_instructions(brand_text)
                assert brand_rules.can_confirm
                brand_confirmation = ConfirmedInstructions(
                    raw_text=brand_text, rules=brand_rules.rules,
                )
                brand_quote = quote_a.create(
                    version_a, _request(2), "brand-confirmed",
                    instructions=brand_confirmation,
                )
                assert brand_quote.ready_for_draft
                assert brand_quote.lines[0].product_brand == "Test"
                assert brand_quote.total_rub == Decimal("43.78")
                assert quote_a.get(brand_quote.quote_id) == brand_quote

                excluded_text = "Исключи бренд Test."
                excluded_rules = propose_instructions(excluded_text)
                assert excluded_rules.can_confirm
                exclusion = ConfirmedInstructions(
                    raw_text=excluded_text, rules=excluded_rules.rules,
                )
                excluded_quote = quote_a.create(
                    version_a, _request(2), "brand-excluded",
                    instructions=exclusion,
                )
                assert not excluded_quote.ready_for_draft
                assert excluded_quote.total_rub is None
                assert excluded_quote.lines[0].reason_codes == ("brand_policy_excluded",)
                assert excluded_quote.lines[0].sku is None
                assert quote_a.get(excluded_quote.quote_id) == excluded_quote
                with pytest.raises(ValueError, match="manager_selection_violates_brand_policy"):
                    quote_a.create(
                        version_a, _request(2), "brand-forbidden-manual",
                        [ManagerSkuSelection(line_number=1, sku="PENCIL-1")],
                        instructions=exclusion,
                    )

                other = quote_b.create(version_b, _request(2), "same-quote-key")
                assert other.quote_id != first.quote_id
                assert other.total_rub == Decimal("79.80")
                assert quote_b.get(other.quote_id) == other
                selected_b = quote_b.create(
                    version_b, _request(2), "selected-b",
                    [ManagerSkuSelection(line_number=1, sku="PENCIL-1")],
                )
                assert selected_b.lines[0].status == "manager_selected"
                assert selected_b.lines[0].unit_price_rub == Decimal("39.90")
                assert selected_b.total_rub == Decimal("79.80")
                with pytest.raises(QuoteNotFound):
                    quote_a.get(selected_b.quote_id)
                with pytest.raises(QuoteNotFound):
                    quote_b.get(selected_a.quote_id)
                with pytest.raises(QuoteNotFound):
                    quote_b.get(first.quote_id)
                with pytest.raises(QuoteNotFound):
                    quote_a.get(other.quote_id)
                with pytest.raises(TenantAccessDenied):
                    viewer_a.create(version_a, _request(3), "viewer-cannot-write")
                # Foreign quote is invisible to the initiating tenant.
                with pytest.raises(QuoteNotFound):
                    quote_a.revise(
                        other.quote_id, 1, version_a, _request(3), "foreign-edit",
                    )
                # The composite tenant FKs must reject foreign quote IDs
                # even when the app role binds a VALID owner of org A.
                _ = quote_a.get(first.quote_id)
                with pytest.raises(DBAPIError), connection.begin_nested():
                    connection.execute(text("""
                        INSERT INTO quote_revisions (
                            organization_id, quote_id, number, catalog_version_id,
                            snapshot, snapshot_sha256, is_complete, total_rub
                        ) VALUES (
                            :org, :quote, 3, :version,
                            '{}'::jsonb, :sha, false, NULL
                        )
                    """), {
                        "org": org_a, "quote": other.quote_id,
                        "version": version_a, "sha": "f" * 64,
                    })
                with pytest.raises(DBAPIError), connection.begin_nested():
                    connection.execute(text("""
                        INSERT INTO quote_write_requests (
                            organization_id, key, request_sha256, quote_id,
                            revision_number
                        ) VALUES (:org, 'foreign-fk', :sha, :quote, 1)
                    """), {
                        "org": org_a, "quote": other.quote_id,
                        "sha": "e" * 64,
                    })
                # A client forged identity must also clear the RLS session
                # context in the CURRENT transaction, not just raise.
                impostor = TenantQuoteRepository(
                    connection, AuthenticatedUser(viewer), org_a,
                    session_token=token_a.session_token,
                )
                with pytest.raises(TenantAccessDenied):
                    impostor.get(first.quote_id)
                assert connection.scalar(text(
                    "SELECT count(*) FROM quote_documents"
                )) == 0
                assert quote_a.get(first.quote_id) == first

                with pytest.raises(QuoteConflict, match="idempotency_conflict"):
                    quote_a.create(version_a, _request(3), "same-quote-key")

                revised = quote_a.revise(
                    first.quote_id, 1, version_a, _request(3), "revise-1",
                )
                assert revised.revision == 2
                assert revised.total_rub == Decimal("59.70")
                assert quote_a.get(first.quote_id) == revised
                assert quote_a.get(first.quote_id, revision=1) == first
                assert quote_a.revise(
                    first.quote_id, 1, version_a, _request(3), "revise-1",
                ) == revised
                with pytest.raises(QuoteConflict, match="stale_revision"):
                    quote_a.revise(
                        first.quote_id, 1, version_a, _request(4), "revise-stale",
                    )
                assert quote_b.get(other.quote_id) == other

                # Immutable revisions cannot be edited or deleted by app role.
                with pytest.raises(DBAPIError), connection.begin_nested():
                    connection.execute(text("""
                        UPDATE quote_revisions SET snapshot_sha256 = :sha
                        WHERE organization_id=:org AND quote_id=:quote AND number=1
                    """), {"sha": "f" * 64, "org": org_a, "quote": first.quote_id})
                with pytest.raises(DBAPIError), connection.begin_nested():
                    connection.execute(text("""
                        DELETE FROM quote_revisions
                        WHERE organization_id=:org AND quote_id=:quote AND number=1
                    """), {"org": org_a, "quote": first.quote_id})
                # Original quote remains valid even after catalog refresh.
                refresh = catalog_a.publish(
                    parsed_b, "updated-catalog", parsed_b.source_sha256,
                )
                assert refresh.id != version_a
                assert quote_a.get(first.quote_id, revision=1) == first
                assert quote_a.get(first.quote_id, revision=2) == revised
                with pytest.raises(QuoteConflict, match="stale_catalog"):
                    quote_a.revise(
                        first.quote_id, 2, version_a, _request(4), "revise-old-catalog",
                    )
                # A new quote can be unresolved without inventing a total.
                incomplete = quote_a.create(
                    refresh.id, _request(None), "quote-incomplete",
                )
                assert not incomplete.ready_for_draft
                assert incomplete.total_rub is None

                connection.execute(text("RESET ROLE"))
                assert connection.scalar(text("""
                    SELECT count(*) FROM quote_revisions
                    WHERE organization_id=:org AND quote_id=:quote
                """), {"org": org_a, "quote": first.quote_id}) == 2
            finally:
                transaction.rollback()
        with engine.connect() as verified:
            assert verified.scalar(text(
                "SELECT version_num FROM alembic_version"
            )) == "0006_legacy_quarantine"
            assert verified.scalar(text("""
                SELECT to_regprocedure(
                    'public.kpdesk_session_tenant_allowed(uuid,text)'
                )
            """)) is None
            assert quote_a_id is not None
            assert verified.scalar(text("""
                SELECT count(*) FROM quote_documents WHERE id=:id
            """), {"id": quote_a_id}) == 0
            assert verified.scalar(text("""
                SELECT count(*) FROM organizations WHERE id=:org
            """), {"org": org_a}) == 0
    finally:
        engine.dispose()
