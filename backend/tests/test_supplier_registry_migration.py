"""0036 registry migration requires explicit approval and verified target."""
from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

migration = importlib.import_module("migrations.versions.0036_supplier_registry")


@pytest.mark.parametrize("action", ("upgrade", "downgrade"))
def test_migration_does_not_access_database_without_approval(
    monkeypatch: pytest.MonkeyPatch, action: str,
) -> None:
    conn = Mock()
    monkeypatch.setattr(migration.op, "get_context", lambda: SimpleNamespace(
        config=SimpleNamespace(attributes={}),
    ))
    monkeypatch.setattr(migration.op, "get_bind", lambda: conn)
    with pytest.raises(RuntimeError, match="explicit Alembic approval"):
        getattr(migration, action)()
    conn.execute.assert_not_called()


@pytest.mark.parametrize(("db", "role", "revision"), [
    ("some_database", "kpdesk", "0035_supplier_source_discovery"),
    ("kpdesk", "kpdesk_tenant_runtime", "0035_supplier_source_discovery"),
    ("kpdesk", "kpdesk", "0034_reviewed_source_bridge"),
])
def test_migration_rejects_wrong_scope_without_ddl(
    monkeypatch: pytest.MonkeyPatch, db: str, role: str, revision: str,
) -> None:
    conn = Mock()
    conn.in_transaction.return_value = True
    conn.execute.return_value.one.return_value = (db, role, revision)
    monkeypatch.setattr(migration.op, "get_context", lambda: SimpleNamespace(
        config=SimpleNamespace(attributes={
            "kpdesk_supplier_registry_0036_approved": True,
        }),
    ))
    monkeypatch.setattr(migration.op, "get_bind", lambda: conn)
    monkeypatch.setattr(migration, "is_live_accounts_http_clone", lambda _: False)
    with pytest.raises(RuntimeError, match="target is unsafe"):
        migration.upgrade()
    assert conn.execute.call_count == 1


def test_ddl_is_for_non_price_evidence_directory_only() -> None:
    from kpdesk.db.supplier_registry_sql import (
        FUNCTION_LIST,
        FUNCTION_STATUS,
        FUNCTION_UPSERT,
    )

    assert "ON CONFLICT DO NOTHING" in FUNCTION_UPSERT
    assert "r.revision=p_expected_revision" in FUNCTION_UPSERT
    assert "kpdesk_session_tenant_allowed(p_org,'write')" in FUNCTION_UPSERT
    assert "kpdesk_session_tenant_allowed(p_org,'read')" in FUNCTION_LIST
    assert "kpdesk_session_tenant_allowed(p_org,'read')" in FUNCTION_STATUS
    for ddl in (FUNCTION_LIST, FUNCTION_STATUS, FUNCTION_UPSERT):
        assert "SECURITY DEFINER" in ddl
        assert "SET search_path=pg_catalog,pg_temp" in ddl
        assert "catalog_items" not in ddl
        assert "a79_catalog_review_provenance" not in ddl
