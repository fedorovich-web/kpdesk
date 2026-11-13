"""Schema contract for A1 tenant foundation on a fresh marked clone."""

import os

import pytest
from postgres_clone_connection import LEGACY_TEST_OPT_IN, legacy_test_database
from sqlalchemy import inspect, text

pytestmark = pytest.mark.skipif(
    os.environ.get(LEGACY_TEST_OPT_IN) != "1",
    reason="Explicit opt-in required for per-test marked disposable PostgreSQL clones",
)


def test_tenant_expand_schema_is_additive_and_scoped_to_test_db() -> None:
    with legacy_test_database() as engine, engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0005_tenant_expand"
        inspector = inspect(connection)
        required_tables = {
            "organizations", "users", "organization_memberships",
            "catalog_versions", "catalog_items", "catalog_active",
            "idempotency_records", "quote_documents", "quote_revisions",
            "quote_write_requests",
        }
        assert required_tables.issubset(set(inspector.get_table_names()))
        for table in required_tables - {"organizations", "users", "organization_memberships"}:
            fields = {col["name"]: col for col in inspector.get_columns(table)}
            assert fields["organization_id"]["nullable"] is True
            assert any(
                fk["constrained_columns"] == ["organization_id"]
                and fk["referred_table"] == "organizations"
                for fk in inspector.get_foreign_keys(table)
            ), table
            assert any(
                "organization_id" in idx["column_names"]
                for idx in inspector.get_indexes(table)
            ), table
        membership_pk = inspector.get_pk_constraint("organization_memberships")
        assert set(membership_pk["constrained_columns"]) == {
            "organization_id", "user_id",
        }
        assert {col["name"] for col in inspector.get_columns("users")} >= {
            "id", "email", "password_hash", "email_verified_at", "status", "created_at",
        }
        assert {col["name"] for col in inspector.get_columns("organizations")} >= {
            "id", "name", "status", "created_at",
        }
        assert {col["name"] for col in inspector.get_columns("organization_memberships")} >= {
            "organization_id", "user_id", "role", "status", "created_at",
        }
        assert any(
            c["name"] == "ck_membership_role"
            for c in inspector.get_check_constraints("organization_memberships")
        )
        assert connection.scalar(text(
            "SELECT count(*) FROM pg_trigger "
            "WHERE tgname='quote_revision_no_update_delete' AND NOT tgisinternal"
        )) == 1
