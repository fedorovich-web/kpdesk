"""Operator approval and vetted target are checked before universal schema DDL."""
import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def test_universal_migration_refuses_without_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    migration=importlib.import_module('migrations.versions.0044_universal_products')
    connection=Mock()
    monkeypatch.setattr(migration.op,'get_context',lambda:SimpleNamespace(config=SimpleNamespace(attributes={})))
    monkeypatch.setattr(migration.op,'get_bind',lambda:connection)
    with pytest.raises(RuntimeError,match='explicit approval'):
        migration.upgrade()
    connection.execute.assert_not_called()


@pytest.mark.parametrize('database,role,previous',[
    ('unmarked','kpdesk','0043_proposal_shares'),
    ('kpdesk','kpdesk_tenant_runtime','0043_proposal_shares'),
    ('kpdesk','kpdesk','0042_openrouter_budget'),
])
def test_universal_migration_refuses_wrong_target(monkeypatch: pytest.MonkeyPatch,database: str,role: str,previous: str) -> None:
    migration=importlib.import_module('migrations.versions.0044_universal_products')
    connection=Mock()
    connection.in_transaction.return_value=True
    connection.execute.return_value.one.return_value=(database,role,previous)
    monkeypatch.setattr(migration.op,'get_context',lambda:SimpleNamespace(config=SimpleNamespace(
        attributes={'kpdesk_universal_0044_approved':True})))
    monkeypatch.setattr(migration.op,'get_bind',lambda:connection)
    monkeypatch.setattr(migration,'is_live_accounts_http_clone',lambda _:False)
    with pytest.raises(RuntimeError,match='Unsafe'):
        migration.upgrade()
    assert connection.execute.call_count==1
