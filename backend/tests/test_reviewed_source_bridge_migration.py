"""Fail-closed rollout guard and DDL for read-only reviewed source bridge."""
from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

migration = importlib.import_module("migrations.versions.0034_reviewed_source_bridge")


@pytest.mark.parametrize("action", ["upgrade", "downgrade"])
def test_bridge_requires_explicit_alembic_approval_before_db_access(
    action: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = Mock()
    monkeypatch.setattr(migration.op, "get_context", lambda: SimpleNamespace(
        config=SimpleNamespace(attributes={}),
    ))
    monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
    with pytest.raises(RuntimeError, match="explicit Alembic approval"):
        getattr(migration, action)()
    connection.execute.assert_not_called()


@pytest.mark.parametrize(("database", "role", "version"), [
    ("other_db", "kpdesk", "0033_reviewed_catalog_provenance"),
    ("kpdesk", "kpdesk_tenant_runtime", "0033_reviewed_catalog_provenance"),
    ("kpdesk", "kpdesk", "0032_supplier_quote_audit"),
])
def test_bridge_rejects_wrong_owner_database_or_revision(
    monkeypatch: pytest.MonkeyPatch, database: str, role: str, version: str,
) -> None:
    connection = Mock()
    connection.in_transaction.return_value = True
    connection.execute.return_value.one.return_value = (database, role, version)
    monkeypatch.setattr(migration.op, "get_context", lambda: SimpleNamespace(
        config=SimpleNamespace(
            attributes={"kpdesk_review_bridge_0034_approved": True},
        ),
    ))
    monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
    monkeypatch.setattr(migration, "is_live_accounts_http_clone", lambda _: False)
    with pytest.raises(RuntimeError, match="target is unsafe"):
        migration.upgrade()
    assert connection.execute.call_count == 1


def test_bridge_grants_read_only_function_to_tenant_not_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = Mock()
    connection.scalar.return_value = None
    monkeypatch.setattr(migration, "_preflight", lambda _: connection)
    migration.upgrade()
    executed = [str(call.args[0]) for call in connection.execute.call_args_list]
    ddl = "\n".join(executed)
    assert "SECURITY DEFINER" in ddl
    assert "kpdesk_session_tenant_allowed(p_org,'read')" in ddl
    assert "p.mapping_sha256 IN (p_mapping_reject,p_mapping_quarantine)" in ddl
    assert "p.canonical_csv_sha256=p_canonical_sha256" in ddl
    assert "p.original_source_sha256=p_original_sha256" in ddl
    assert "p.price_basis='standard'" in ddl
    assert "GRANT EXECUTE ON FUNCTION" in ddl
    assert "TO kpdesk_tenant_runtime" in ddl
    assert not any("GRANT SELECT" in statement or "INSERT INTO" in statement
                   for statement in executed)
