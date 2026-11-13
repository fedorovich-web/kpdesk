"""Offline guards for the explicitly approved credential/reset serialization patch."""
import ast
import importlib
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

MODULE = 'migrations.versions.0047_auth_reset_serialization'
APPROVAL = 'kpdesk_auth_reset_serialization_0047_approved'
PREVIOUS = '0046_provider_authority'
CURRENT = '0047_auth_reset_serialization'
ORIGINAL = 'FROM public.users u WHERE u.email=p_email;'
LOCKED = 'FROM public.users u WHERE u.email=p_email FOR UPDATE OF u;'


def original_body() -> str:
    path = Path(__file__).parents[1] / 'migrations/versions/0014_live_auth_rls.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    definitions = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
                   and isinstance(node.value, str)
                   and 'CREATE FUNCTION public.kpdesk_auth_credential(' in node.value]
    assert len(definitions) == 1
    return definitions[0].split('AS $$', 1)[1].rsplit('$$;', 1)[0]


def installed(*, locked: bool = False) -> dict[str, object]:
    body = original_body()
    if locked:
        body = body.replace(ORIGINAL, LOCKED)
    definition = (
        'CREATE OR REPLACE FUNCTION public.kpdesk_auth_credential(p_email text, p_peer text)\n'
        ' RETURNS TABLE(identity_id uuid, secret_hash text, usable boolean, limited boolean)\n'
        " LANGUAGE plpgsql\n SECURITY DEFINER\n SET search_path TO 'pg_catalog', 'pg_temp'\n"
        'AS $function$' + body + '$function$\n'
    )
    return {'source': body, 'definition': definition, 'security_definer': True,
            'configuration': ['search_path=pg_catalog, pg_temp'], 'language': 'plpgsql',
            'owner': 'kpdesk', 'identity_arguments': 'p_email text, p_peer text',
            'result': 'TABLE(identity_id uuid, secret_hash text, usable boolean, limited boolean)'}


def setup_migration(monkeypatch: pytest.MonkeyPatch, *, action: str = 'upgrade',
                    approval: object = True, database: str = 'kpdesk', role: str = 'kpdesk',
                    previous: str | None = None, transaction: bool = True,
                    marked: bool = False, metadata: dict[str, object] | None = None,
                    ) -> tuple[ModuleType, Mock, Mock]:
    migration = importlib.import_module(MODULE)
    connection = Mock()
    connection.in_transaction.return_value = transaction
    connection.execute.return_value.one.return_value = (
        database, role, previous or (PREVIOUS if action == 'upgrade' else CURRENT),
    )
    connection.execute.return_value.mappings.return_value.one_or_none.return_value = (
        installed(locked=action == 'downgrade') if metadata is None else metadata
    )
    clone = Mock(return_value=marked)
    monkeypatch.setattr(migration, 'is_live_accounts_http_clone', clone)
    monkeypatch.setattr(migration.op, 'get_context', lambda: SimpleNamespace(
        config=SimpleNamespace(attributes={APPROVAL: approval})))
    monkeypatch.setattr(migration.op, 'get_bind', lambda: connection)
    return migration, connection, clone


def mutations(connection: Mock) -> list[str]:
    return [str(call.args[0]) for call in connection.execute.call_args_list
            if str(call.args[0]).lstrip().startswith('CREATE OR REPLACE FUNCTION')]


@pytest.mark.parametrize('action', ['upgrade', 'downgrade'])
@pytest.mark.parametrize('approval', [None, False, 1, 'true'])
def test_missing_explicit_approval_never_connects(
    monkeypatch: pytest.MonkeyPatch, action: str, approval: object,
) -> None:
    migration, connection, _ = setup_migration(monkeypatch, action=action, approval=approval)
    get_bind = Mock(return_value=connection)
    monkeypatch.setattr(migration.op, 'get_bind', get_bind)
    with pytest.raises(RuntimeError, match='explicit approval'):
        getattr(migration, action)()
    get_bind.assert_not_called()


@pytest.mark.parametrize('action', ['upgrade', 'downgrade'])
@pytest.mark.parametrize('database,role,previous,transaction,marked', [
    ('other', 'kpdesk', None, True, False),
    ('kpdesk_accounts_http_123456789abc', 'kpdesk', None, True, False),
    ('kpdesk', 'kpdesk_tenant_runtime', None, True, False),
    ('kpdesk', 'kpdesk', '0045_registry_retirement', True, False),
    ('kpdesk', 'kpdesk', None, False, False),
])
def test_wrong_scope_refuses_function_changes(
    monkeypatch: pytest.MonkeyPatch, action: str, database: str, role: str,
    previous: str | None, transaction: bool, marked: bool,
) -> None:
    migration, connection, _ = setup_migration(monkeypatch, action=action, database=database,
        role=role, previous=previous, transaction=transaction, marked=marked)
    with pytest.raises(RuntimeError, match='Unsafe auth reset serialization migration target'):
        getattr(migration, action)()
    assert mutations(connection) == []


@pytest.mark.parametrize('action', ['upgrade', 'downgrade'])
@pytest.mark.parametrize('field,value', [
    ('source', 'BEGIN RETURN; END'), ('security_definer', False),
    ('configuration', ['search_path=public']), ('language', 'sql'), ('owner', 'other'),
    ('identity_arguments', 'p_email text'), ('result', 'text'),
    ('definition', 'CREATE OR REPLACE FUNCTION public.other() RETURNS void LANGUAGE sql AS $$SELECT 1$$'),
])
def test_unexpected_installed_function_fails_closed(
    monkeypatch: pytest.MonkeyPatch, action: str, field: str, value: object,
) -> None:
    metadata = installed(locked=action == 'downgrade')
    metadata[field] = value
    migration, connection, _ = setup_migration(monkeypatch, action=action, metadata=metadata)
    with pytest.raises(RuntimeError, match='Unexpected installed authentication gateway'):
        getattr(migration, action)()
    assert mutations(connection) == []


@pytest.mark.parametrize('action', ['upgrade', 'downgrade'])
def test_upgrade_and_explicit_security_rollback_change_only_final_lookup(
    monkeypatch: pytest.MonkeyPatch, action: str,
) -> None:
    before = installed(locked=action == 'downgrade')
    migration, connection, clone = setup_migration(monkeypatch, action=action, metadata=before)
    getattr(migration, action)()
    actual = mutations(connection)
    old, new = (ORIGINAL, LOCKED) if action == 'upgrade' else (LOCKED, ORIGINAL)
    assert len(actual) == 2
    assert actual[0] == str(before['definition']).replace(old, new)
    assert actual[0].count('FOR UPDATE OF u') == (1 if action == 'upgrade' else 0)
    assert 'SECURITY DEFINER' in actual[0]
    assert "SET search_path TO 'pg_catalog', 'pg_temp'" in actual[0]
    assert 'account_count>6 OR peer_count>30' in actual[0]
    assert "interval '15 minutes'" in actual[0]
    assert 'CREATE OR REPLACE FUNCTION public.kpdesk_runtime_schema_revision()' in actual[1]
    assert 'SECURITY DEFINER SET search_path=pg_catalog,pg_temp' in actual[1]
    assert f"SELECT '{CURRENT if action == 'upgrade' else PREVIOUS}'::text" in actual[1]
    assert all(word not in statement for statement in actual
               for word in ('GRANT ', 'REVOKE ', 'ALTER ROLE', 'UPDATE public.users'))
    clone.assert_not_called()
    assert migration.revision == CURRENT and migration.down_revision == PREVIOUS
    statements = [str(call.args[0]) for call in connection.execute.call_args_list]
    assert statements.index("SET LOCAL lock_timeout='5s'") < statements.index(actual[0])


@pytest.mark.parametrize('action', ['upgrade', 'downgrade'])
def test_checked_marked_clone_is_accepted(monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    migration, connection, clone = setup_migration(monkeypatch, action=action,
        database='kpdesk_accounts_http_123456789abc', marked=True)
    getattr(migration, action)()
    clone.assert_called_once_with(connection)
    assert len(mutations(connection)) == 2


def test_rate_limit_or_lookup_drift_is_not_silently_overwritten(monkeypatch: pytest.MonkeyPatch) -> None:
    metadata = installed()
    metadata['source'] = str(metadata['source']).replace('account_count>6', 'account_count>99')
    migration, connection, _ = setup_migration(monkeypatch, metadata=metadata)
    with pytest.raises(RuntimeError, match='Unexpected installed authentication gateway'):
        migration.upgrade()
    assert mutations(connection) == []
