import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

migration = importlib.import_module("migrations.versions.0039_company_logo")


@pytest.mark.parametrize("action", ["upgrade", "downgrade"])
def test_logo_migration_requires_explicit_approval_before_connecting(monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    connection = Mock()
    monkeypatch.setattr(migration.op, "get_context", lambda: SimpleNamespace(config=SimpleNamespace(attributes={})))
    monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
    with pytest.raises(RuntimeError, match="explicit approval"):
        getattr(migration, action)()
    connection.execute.assert_not_called()


def test_logo_migration_refuses_unknown_database_and_lossy_downgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = Mock(); connection.in_transaction.return_value = True
    monkeypatch.setattr(migration.op, "get_context", lambda: SimpleNamespace(config=SimpleNamespace(attributes={"kpdesk_company_logo_0039_approved": True})))
    monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
    monkeypatch.setattr(migration, "is_live_accounts_http_clone", lambda _: False)
    connection.execute.return_value.one.return_value = ("unmarked", "kpdesk", migration.down_revision)
    with pytest.raises(RuntimeError, match="Unsafe"):
        migration.upgrade()
    connection.execute.return_value.one.return_value = ("kpdesk", "kpdesk", migration.revision)
    connection.scalar.return_value = True
    with pytest.raises(RuntimeError, match="Business data"):
        migration.downgrade()
    assert not any("ALTER TABLE" in str(call.args[0]) for call in connection.execute.call_args_list)
