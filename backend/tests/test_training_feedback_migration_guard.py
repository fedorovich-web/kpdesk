"""Migration0040 remains opt-in and refuses unsafe targets without DDL."""
import importlib
from unittest.mock import Mock

import pytest

migration = importlib.import_module('migrations.versions.0040_training_feedback')


@pytest.mark.parametrize('action', ['upgrade', 'downgrade'])
def test_feedback_migration_needs_separate_rollout_permission(monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    config = Mock(attributes={})
    get_bind = Mock()
    monkeypatch.setattr(migration.op, 'get_context', lambda: Mock(config=config))
    monkeypatch.setattr(migration.op, 'get_bind', get_bind)
    with pytest.raises(RuntimeError, match='explicit approval'):
        getattr(migration, action)()
    get_bind.assert_not_called()


@pytest.mark.parametrize('database,role,previous,transaction,marker', [
    ('unknown', 'kpdesk', '0039_company_logo', True, False),
    ('kpdesk_accounts_http_123456789abc', 'kpdesk', '0039_company_logo', True, False),
    ('kpdesk', 'kpdesk_tenant_runtime', '0039_company_logo', True, False),
    ('kpdesk', 'kpdesk', '0031_catalog_taxonomy_v4', True, False),
    ('kpdesk', 'kpdesk', '0039_company_logo', False, False),
])
def test_feedback_migration_rejects_invalid_scope_before_ddl(
    monkeypatch: pytest.MonkeyPatch, database: str, role: str, previous: str, transaction: bool, marker: bool,
) -> None:
    connection = Mock()
    connection.in_transaction.return_value = transaction
    connection.execute.return_value.one.return_value = (database, role, previous)
    monkeypatch.setattr(migration.op, 'get_context', lambda: Mock(config=Mock(attributes={
        'kpdesk_training_feedback_0040_approved': True})))
    monkeypatch.setattr(migration.op, 'get_bind', lambda: connection)
    monkeypatch.setattr(migration, 'is_live_accounts_http_clone', lambda unused: marker)
    create = Mock()
    monkeypatch.setattr(migration, 'create_training_feedback_objects', create)
    with pytest.raises(RuntimeError, match='Unsafe feedback migration target'):
        migration.upgrade()
    create.assert_not_called()
