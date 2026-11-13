import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

migration = importlib.import_module("migrations.versions.0037_company_profile")


@pytest.mark.parametrize("action", ["upgrade", "downgrade"])
def test_profile_migration_does_not_connect_without_approval(monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    connection = Mock()
    monkeypatch.setattr(migration.op, "get_context", lambda: SimpleNamespace(config=SimpleNamespace(attributes={})))
    monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
    with pytest.raises(RuntimeError, match="explicit approval"):
        getattr(migration, action)()
    connection.execute.assert_not_called()


def test_profile_migration_refuses_unknown_target_and_nonempty_downgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = Mock(); conn.in_transaction.return_value = True
    conn.execute.return_value.one.return_value = ("unmarked", "kpdesk", migration.down_revision)
    monkeypatch.setattr(migration.op, "get_context", lambda: SimpleNamespace(config=SimpleNamespace(attributes={
        "kpdesk_company_profile_0037_approved": True})))
    monkeypatch.setattr(migration.op, "get_bind", lambda: conn)
    monkeypatch.setattr(migration, "is_live_accounts_http_clone", lambda _: False)
    with pytest.raises(RuntimeError, match="Unsafe"):
        migration.upgrade()
    conn.execute.return_value.one.return_value = ("kpdesk", "kpdesk", migration.revision)
    conn.scalar.return_value = 1
    with pytest.raises(RuntimeError, match="business data"):
        migration.downgrade()
    assert not any("DROP" in str(call.args[0]) for call in conn.execute.call_args_list)
