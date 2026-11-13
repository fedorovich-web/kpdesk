"""Retired registry gateways require a gated, non-destructive forward migration."""
import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def test_retirement_never_accesses_database_without_explicit_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = importlib.import_module('migrations.versions.0045_registry_retirement')
    conn = Mock()
    monkeypatch.setattr(migration.op, 'get_context', lambda: SimpleNamespace(config=SimpleNamespace(attributes={})))
    monkeypatch.setattr(migration.op, 'get_bind', lambda: conn)
    with pytest.raises(RuntimeError, match='explicit approval'):
        migration.upgrade()
    conn.execute.assert_not_called()


@pytest.mark.parametrize('database,role,revision,transaction', [
    ('other', 'kpdesk', '0044_universal_products', True),
    ('kpdesk', 'kpdesk_tenant_runtime', '0044_universal_products', True),
    ('kpdesk', 'kpdesk', '0043_proposal_shares', True),
    ('kpdesk', 'kpdesk', '0044_universal_products', False),
])
def test_retirement_refuses_wrong_database_role_revision_or_transaction(
    monkeypatch: pytest.MonkeyPatch, database: str, role: str, revision: str, transaction: bool,
) -> None:
    migration = importlib.import_module('migrations.versions.0045_registry_retirement')
    conn = Mock()
    conn.in_transaction.return_value = transaction
    conn.execute.return_value.one.return_value = (database, role, revision)
    monkeypatch.setattr(migration.op, 'get_context', lambda: SimpleNamespace(config=SimpleNamespace(
        attributes={'kpdesk_registry_retirement_0045_approved': True})))
    monkeypatch.setattr(migration.op, 'get_bind', lambda: conn)
    monkeypatch.setattr(migration, 'is_live_accounts_http_clone', lambda conn: False)
    with pytest.raises(RuntimeError, match='Unsafe registry retirement target'):
        migration.upgrade()
    assert conn.execute.call_count == 1


def test_retirement_never_restores_legacy_write_authority_on_downgrade() -> None:
    migration = importlib.import_module('migrations.versions.0045_registry_retirement')
    with pytest.raises(RuntimeError, match='separately reviewed'):
        migration.downgrade()


@pytest.mark.parametrize('retired', [True, False])
def test_0045_readiness_requires_retirement_and_existing_0044_guards(
    monkeypatch: pytest.MonkeyPatch, retired: bool,
) -> None:
    from test_release_runtime import engines

    from kpdesk.release_runtime import verify_release_schema

    pair = engines('0045_registry_retirement')
    pair[1].begin.return_value.__enter__.return_value.scalar.return_value = True
    monkeypatch.setattr('kpdesk.db.supplier_registry_schema.registry_retired', lambda conn: retired)
    if retired:
        verify_release_schema(pair[1])
    else:
        with pytest.raises(RuntimeError, match='registry'):
            verify_release_schema(pair[1])


@pytest.mark.parametrize('available', ['sharing', 'universal'])
def test_0045_cannot_bypass_previous_release_guards(monkeypatch: pytest.MonkeyPatch, available: str) -> None:
    from test_release_runtime import engines

    from kpdesk.release_runtime import verify_release_schema

    pair = engines('0045_registry_retirement')
    pair[1].begin.return_value.__enter__.return_value.scalar.return_value = True
    monkeypatch.setattr('kpdesk.db.supplier_registry_schema.registry_retired', lambda conn: True)
    module = 'kpdesk.quotes.sharing.sharing_available' if available == 'sharing' else 'kpdesk.db.universal_schema.universal_available'
    monkeypatch.setattr(module, lambda conn: False)
    with pytest.raises(RuntimeError, match='schema'):
        verify_release_schema(pair[1])
