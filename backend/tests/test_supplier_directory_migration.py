"""0036 supplier registry must never upgrade without explicit owner approval."""
from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

migration = importlib.import_module("migrations.versions.0036_supplier_registry")


@pytest.mark.parametrize("action", ("upgrade", "downgrade"))
def test_no_approval_makes_no_database_calls(
    monkeypatch: pytest.MonkeyPatch, action: str,
) -> None:
    connection = Mock()
    monkeypatch.setattr(migration.op, "get_context", lambda: SimpleNamespace(
        config=SimpleNamespace(attributes={}),
    ))
    monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
    with pytest.raises(RuntimeError, match="explicit Alembic approval"):
        getattr(migration, action)()
    connection.execute.assert_not_called()


@pytest.mark.parametrize(("db", "role", "revision"), [
    ("unexpected_database", "kpdesk", "0035_supplier_source_discovery"),
    ("kpdesk", "kpdesk_tenant_runtime", "0035_supplier_source_discovery"),
    ("kpdesk", "kpdesk", "0034_reviewed_source_bridge"),
])
def test_bad_target_preflight_never_runs_ddl(
    monkeypatch: pytest.MonkeyPatch, db: str, role: str, revision: str,
) -> None:
    connection = Mock()
    connection.in_transaction.return_value = True
    connection.execute.return_value.one.return_value = (db, role, revision)
    monkeypatch.setattr(migration.op, "get_context", lambda: SimpleNamespace(
        config=SimpleNamespace(attributes={
            "kpdesk_supplier_registry_0036_approved": True,
        }),
    ))
    monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
    monkeypatch.setattr(migration, "is_live_accounts_http_clone", lambda _: False)
    with pytest.raises(RuntimeError, match="target is unsafe"):
        migration.upgrade()
    assert connection.execute.call_count == 1


def test_existing_registry_blocks_downgrade_of_business_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = Mock()
    connection.scalar.return_value = 1
    monkeypatch.setattr(migration, "_preflight", lambda _: connection)
    with pytest.raises(RuntimeError, match="contains business data"):
        migration.downgrade()
    connection.execute.assert_not_called()
