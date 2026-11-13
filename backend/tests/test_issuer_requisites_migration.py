import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

migration = importlib.import_module("migrations.versions.0038_issuer_requisites")


@pytest.mark.parametrize("action", ["upgrade", "downgrade"])
def test_sender_migration_requires_explicit_approval_before_db(monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    connection = Mock()
    monkeypatch.setattr(migration.op, "get_context", lambda: SimpleNamespace(config=SimpleNamespace(attributes={})))
    monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
    with pytest.raises(RuntimeError, match="explicit approval"):
        getattr(migration, action)()
    connection.execute.assert_not_called()


def test_sender_migration_refuses_unknown_database_and_lossy_downgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = Mock()
    connection.in_transaction.return_value = True
    connection.execute.return_value.one.return_value = ("unknown", "kpdesk", migration.down_revision)
    monkeypatch.setattr(migration.op, "get_context", lambda: SimpleNamespace(config=SimpleNamespace(attributes={
        "kpdesk_issuer_requisites_0038_approved": True})))
    monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
    monkeypatch.setattr(migration, "is_live_accounts_http_clone", lambda _: False)
    with pytest.raises(RuntimeError, match="Unsafe"):
        migration.upgrade()
    connection.execute.return_value.one.return_value = ("kpdesk", "kpdesk", migration.revision)
    connection.scalar.return_value = 1
    with pytest.raises(RuntimeError, match="sender requisites"):
        migration.downgrade()
    assert not any("CREATE OR REPLACE" in str(call.args[0]) for call in connection.execute.call_args_list)
