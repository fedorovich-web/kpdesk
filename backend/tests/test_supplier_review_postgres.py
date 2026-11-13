"""Transaction-only A3→A4 PostgreSQL tenant review schema verification.

No persistent schema/data/role changes anywhere; no production database DDL.
"""
from __future__ import annotations

import importlib
import uuid
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from dotenv import dotenv_values
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError, IntegrityError

from kpdesk.catalog.supplier_inference import inspect_supplier_source
from kpdesk.catalog.supplier_profiles import approve_supplier_profile
from kpdesk.catalog.supplier_review_storage import TenantSupplierProfileStore
from kpdesk.db.tenant_access import AuthenticatedUser, TenantAccessDenied

ROOT = Path(__file__).resolve().parents[2]


def _test_engine(*, database: str, read_only: bool = False) -> Engine:
    env = dotenv_values(ROOT / ".env")
    password = env.get("KP_DESK_DB_PASSWORD")
    assert isinstance(password, str) and len(password) >= 32
    return create_engine(URL.create(
        "postgresql+psycopg", username="kpdesk", password=password,
        host="127.0.0.1", port=int(env.get("KP_DESK_DB_PORT") or 55432),
        database=database,
    ), connect_args={"options": "-c default_transaction_read_only=on"} if read_only else {})


def test_a4_supplier_reviews_rls_fk_append_only_grants_all_rollback() -> None:
    engine = _test_engine(database="kpdesk_tenant_test")
    org_a, org_b, person_a, person_b = (uuid.uuid4() for _ in range(4))
    fingerprint = "a" * 64
    sha = "b" * 64
    profile_a_hash = "c" * 64
    profile_b_hash = "d" * 64
    try:
        with engine.connect() as connection:
            txn = connection.begin()
            try:
                assert connection.scalar(text("SELECT current_database()")) == (
                    "kpdesk_tenant_test"
                )
                assert connection.scalar(text(
                    "SELECT version_num FROM alembic_version"
                )) == "0006_legacy_quarantine"
                a3 = importlib.import_module("migrations.versions.0007_tenant_contract_rls")
                a4 = importlib.import_module("migrations.versions.0008_supplier_review_rls")
                with Operations.context(MigrationContext.configure(connection)):
                    a3.upgrade()
                    a4.upgrade()
                for org, reviewer, number in (
                    (org_a, person_a, 1), (org_b, person_b, 2),
                ):
                    connection.execute(text("""
                        INSERT INTO organizations(id,name,status)
                        VALUES(:org,:name,'active')
                    """), {"org": org, "name": f"A4 Review {number}"})
                    connection.execute(text("""
                        INSERT INTO users(id,email,status,email_verified_at)
                        VALUES (:person,:email,'active',now())
                    """), {"person": reviewer, "email": f"reviewer{number}@a4.invalid"})

                for org, person in ((org_a, person_a), (org_b, person_b)):
                    connection.execute(text("""
                        INSERT INTO organization_memberships
                            (organization_id,user_id,role,status)
                        VALUES (:org,:person,'owner','active')
                    """), {"org": org, "person": person})
                for table in ("supplier_reviewed_profiles", "supplier_review_decisions"):
                    assert connection.execute(text("""
                        SELECT relrowsecurity,relforcerowsecurity
                        FROM pg_class WHERE oid=to_regclass(:name)
                    """), {"name": table}).one() == (True, True)
                connection.execute(text("GRANT USAGE ON SCHEMA public TO kpdesk_app"))
                for table in ("organizations", "users", "organization_memberships"):
                    connection.exec_driver_sql(
                        f"GRANT SELECT ON TABLE public.{table} TO kpdesk_app"
                    )
                for table in ("supplier_reviewed_profiles", "supplier_review_decisions"):
                    connection.exec_driver_sql(
                        f"GRANT SELECT, INSERT ON TABLE {table} TO kpdesk_app"
                    )
                connection.execute(text("SET ROLE kpdesk_app"))
                assert connection.scalar(text("SELECT current_user")) == "kpdesk_app"
                assert connection.scalar(text(
                    "SELECT count(*) FROM supplier_reviewed_profiles"
                )) == 0

                for org, reviewer, profile_hash in (
                    (org_a, person_a, profile_a_hash),
                    (org_b, person_b, profile_b_hash),
                ):
                    connection.execute(text(
                        "SELECT set_config('kpdesk.organization_id', :org, true)"
                    ), {"org": str(org)})
                    connection.execute(text("""
                        INSERT INTO supplier_reviewed_profiles (
                            organization_id,supplier_id,schema_fingerprint,
                            profile_sha256,source_sha256,approved_by,profile_json
                        ) VALUES (
                            :org,'shop-1',:fingerprint,:profile_hash,:sha,:reviewer,
                            jsonb_build_object('validated', true)
                        )
                    """), {
                        "org": org, "reviewer": reviewer,
                        "fingerprint": fingerprint, "profile_hash": profile_hash,
                        "sha": sha,
                    })
                    assert connection.scalar(text(
                        "SELECT count(*) FROM supplier_reviewed_profiles"
                    )) == 1
                    connection.execute(text("""
                        INSERT INTO supplier_review_decisions (
                            organization_id,supplier_id,schema_fingerprint,
                            profile_sha256,source_sha256,source_line,source_row_sha256,
                            decision_kind,decision_json,reviewed_by
                        ) VALUES (
                            :org,'shop-1',:fingerprint,:profile_hash,:sha,2,:row_hash,
                            'accepted',jsonb_build_object('approved', true),:reviewer
                        )
                    """), {
                        "org": org, "fingerprint": fingerprint,
                        "profile_hash": profile_hash, "sha": sha,
                        "row_hash": "e" * 64, "reviewer": reviewer,
                    })
                    assert connection.scalar(text(
                        "SELECT count(*) FROM supplier_review_decisions"
                    )) == 1

                # A real reviewed mapping is persisted and loaded using the
                # same unprivileged runtime PostgreSQL role and membership.
                original = b"sku;name;price;currency\\nS1;Mouse;500;RUB\\n"
                inspected = inspect_supplier_source("verified.csv", original)
                approved = approve_supplier_profile(
                    inspected, organization_id=org_a, supplier_id="shop-reviewed",
                    reviewer=AuthenticatedUser(person_a),
                    source_sha256=inspected.source_sha256,
                    selected_columns=inspected.proposed_fields,
                    price_basis="standard", confirmed_currency=None,
                    review_evidence="Human checked original reviewed columns",
                    explicitly_confirmed=True,
                )
                store_a = TenantSupplierProfileStore(
                    connection, AuthenticatedUser(person_a), org_a,
                )
                store_b = TenantSupplierProfileStore(
                    connection, AuthenticatedUser(person_b), org_b,
                )
                assert store_a.save(approved) == approved
                assert store_a.save(approved) == approved
                assert store_a.load("shop-reviewed", approved.fingerprint) == approved
                assert store_b.load("shop-reviewed", approved.fingerprint) is None
                with pytest.raises(TenantAccessDenied):
                    store_b.save(approved)
                connection.execute(text(
                    "SELECT set_config('kpdesk.organization_id', :org, true)"
                ), {"org": str(org_a)})
                assert connection.scalar(text("""
                    SELECT count(*) FROM supplier_reviewed_profiles
                    WHERE profile_sha256 = :hash
                """), {"hash": profile_b_hash}) == 0
                # A source row for org A cannot reference the profile owned by B.
                with pytest.raises(IntegrityError), connection.begin_nested():
                    connection.execute(text("""
                        INSERT INTO supplier_review_decisions (
                            organization_id,supplier_id,schema_fingerprint,
                            profile_sha256,source_sha256,source_line,source_row_sha256,
                            decision_kind,decision_json,reviewed_by
                        ) VALUES (
                            :org,'shop-1',:fingerprint,:profile_hash,:sha,3,:row_hash,
                            'accepted','{}'::jsonb,:reviewer
                        )
                    """), {
                        "org": org_a, "fingerprint": fingerprint,
                        "profile_hash": profile_b_hash, "sha": sha,
                        "row_hash": "f" * 64, "reviewer": person_a,
                    })
                with pytest.raises(IntegrityError), connection.begin_nested():
                    connection.execute(text("""
                        INSERT INTO supplier_review_decisions (
                            organization_id,supplier_id,schema_fingerprint,
                            profile_sha256,source_sha256,source_line,source_row_sha256,
                            decision_kind,decision_json,reviewed_by
                        ) VALUES (
                            :org,'shop-1',:fingerprint,:profile_hash,:sha,3,:row_hash,
                            'excluded',jsonb_build_object('reason', 'bad'),:reviewer
                        )
                    """), {
                        "org": org_a, "fingerprint": fingerprint,
                        "profile_hash": profile_a_hash, "sha": sha,
                        "row_hash": "f" * 64, "reviewer": person_a,
                    })
                # Tenant key RLS cannot be spoofed by INSERT org B while bound to A.
                with pytest.raises(DBAPIError), connection.begin_nested():
                    connection.execute(text("""
                        INSERT INTO supplier_reviewed_profiles (
                            organization_id,supplier_id,schema_fingerprint,
                            profile_sha256,source_sha256,approved_by,profile_json
                        ) VALUES (
                            :org,'shop-2',:fingerprint,:profile_hash,:sha,:reviewer,
                            '{}'::jsonb
                        )
                    """), {
                        "org": org_b, "fingerprint": fingerprint,
                        "profile_hash": "0" * 64, "sha": sha,
                        "reviewer": person_b,
                    })
                # No app permission to tamper with confirmed, append-only evidence.
                with pytest.raises(DBAPIError), connection.begin_nested():
                    connection.execute(text("""
                        UPDATE supplier_reviewed_profiles
                        SET source_sha256 = :new_hash WHERE organization_id = :org
                    """), {"org": org_a, "new_hash": "0" * 64})
                connection.execute(text(
                    "SELECT set_config('kpdesk.organization_id', '', true)"
                ))
                assert connection.scalar(text(
                    "SELECT count(*) FROM supplier_review_decisions"
                )) == 0
                connection.execute(text("RESET ROLE"))
            finally:
                txn.rollback()
        with engine.connect() as verify:
            assert verify.scalar(text(
                "SELECT version_num FROM alembic_version"
            )) == "0006_legacy_quarantine"
            assert verify.scalar(text(
                "SELECT to_regclass('public.supplier_reviewed_profiles')"
            )) is None
            assert verify.scalar(text(
                "SELECT to_regclass('public.supplier_review_decisions')"
            )) is None
            assert verify.scalar(text(
                "SELECT relrowsecurity FROM pg_class "
                "WHERE oid='catalog_versions'::regclass"
            )) is False
            assert verify.scalar(text(
                "SELECT count(*) FROM organizations WHERE id = :org"
            ), {"org": org_a}) == 0
    finally:
        engine.dispose()


def test_a4_review_migration_refuses_production_on_read_only_connection() -> None:
    engine = _test_engine(database="kpdesk", read_only=True)
    migration = importlib.import_module("migrations.versions.0008_supplier_review_rls")
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT current_database()")) == "kpdesk"
            with (
                Operations.context(MigrationContext.configure(connection)),
                pytest.raises(RuntimeError, match="forbidden outside kpdesk_tenant_test"),
            ):
                migration.upgrade()
    finally:
        engine.dispose()
