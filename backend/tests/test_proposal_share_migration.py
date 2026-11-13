import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

migration = importlib.import_module('migrations.versions.0043_proposal_shares')


def test_0043_refuses_unapproved_migration_without_connecting(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = Mock()
    monkeypatch.setattr(migration.op, 'get_context', lambda: SimpleNamespace(config=SimpleNamespace(attributes={})))
    monkeypatch.setattr(migration.op, 'get_bind', lambda: conn)
    with pytest.raises(RuntimeError, match='explicit approval'):
        migration.upgrade()
    conn.execute.assert_not_called()


@pytest.mark.parametrize('database,role,previous', [
    ('unmarked', 'kpdesk', '0042_openrouter_budget'),
    ('kpdesk', 'kpdesk_tenant_runtime', '0042_openrouter_budget'),
    ('kpdesk', 'kpdesk', '0041_runtime_readiness'),
])
def test_0043_refuses_wrong_target_before_ddl(monkeypatch: pytest.MonkeyPatch, database: str, role: str, previous: str) -> None:
    conn = Mock()
    conn.in_transaction.return_value = True
    conn.execute.return_value.one.return_value = (database, role, previous)
    monkeypatch.setattr(migration.op, 'get_context', lambda: SimpleNamespace(config=SimpleNamespace(
        attributes={'kpdesk_proposal_shares_0043_approved': True})))
    monkeypatch.setattr(migration.op, 'get_bind', lambda: conn)
    monkeypatch.setattr(migration, 'is_live_accounts_http_clone', lambda _: False)
    with pytest.raises(RuntimeError, match='Unsafe'):
        migration.upgrade()
    assert len(conn.execute.call_args_list) == 1
