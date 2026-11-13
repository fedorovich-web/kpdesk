"""Provider authority hardening is gated and preserves prior readiness guards."""
import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def test_missing_provider_approval_never_accesses_database(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = importlib.import_module('migrations.versions.0046_provider_authority')
    conn = Mock()
    monkeypatch.setattr(migration.op, 'get_context', lambda: SimpleNamespace(config=SimpleNamespace(attributes={})))
    monkeypatch.setattr(migration.op, 'get_bind', lambda: conn)
    with pytest.raises(RuntimeError, match='explicit approval'):
        migration.upgrade()
    conn.execute.assert_not_called()


@pytest.mark.parametrize('database,role,revision,transaction', [
    ('other', 'kpdesk', '0045_registry_retirement', True),
    ('kpdesk', 'kpdesk_tenant_runtime', '0045_registry_retirement', True),
    ('kpdesk', 'kpdesk', '0044_universal_products', True),
    ('kpdesk', 'kpdesk', '0045_registry_retirement', False),
])
def test_provider_upgrade_refuses_wrong_scope_before_ddl(
    monkeypatch: pytest.MonkeyPatch, database: str, role: str, revision: str, transaction: bool,
) -> None:
    migration = importlib.import_module('migrations.versions.0046_provider_authority')
    conn = Mock()
    conn.in_transaction.return_value = transaction
    conn.execute.return_value.one.return_value = (database, role, revision)
    monkeypatch.setattr(migration.op, 'get_context', lambda: SimpleNamespace(config=SimpleNamespace(
        attributes={'kpdesk_provider_authority_0046_approved': True})))
    monkeypatch.setattr(migration.op, 'get_bind', lambda: conn)
    monkeypatch.setattr(migration, 'is_live_accounts_http_clone', lambda conn: False)
    with pytest.raises(RuntimeError, match='Unsafe provider authority migration target'):
        migration.upgrade()
    assert conn.execute.call_count == 1


def test_provider_downgrade_never_removes_security_guard() -> None:
    migration = importlib.import_module('migrations.versions.0046_provider_authority')
    with pytest.raises(RuntimeError, match='separately reviewed'):
        migration.downgrade()


@pytest.mark.parametrize('retired', [False, True])
def test_0046_keeps_0045_retirement_check(monkeypatch: pytest.MonkeyPatch, retired: bool) -> None:
    from test_release_runtime import engines

    from kpdesk.release_runtime import verify_release_schema

    pair = engines('0046_provider_authority')
    pair[1].begin.return_value.__enter__.return_value.scalar.return_value = True
    monkeypatch.setattr('kpdesk.db.supplier_registry_schema.registry_retired', lambda conn: retired)
    if retired:
        verify_release_schema(pair[1])
    else:
        with pytest.raises(RuntimeError, match='registry'):
            verify_release_schema(pair[1])


def test_0046_rejects_missing_existing_budget_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    from test_release_runtime import engines

    from kpdesk.release_runtime import verify_release_schema

    pair = engines('0046_provider_authority')
    conn = pair[1].begin.return_value.__enter__.return_value
    conn.scalar.return_value = True
    conn.execute.return_value.one.return_value = ('kpdesk_tenant_runtime', '0046_provider_authority', True, False, True)
    monkeypatch.setattr('kpdesk.db.supplier_registry_schema.registry_retired', lambda conn: True)
    with pytest.raises(RuntimeError, match='schema'):
        verify_release_schema(pair[1])
