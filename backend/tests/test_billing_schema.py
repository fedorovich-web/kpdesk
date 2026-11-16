"""Offline security shape checks; no database fixtures or network."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from sqlalchemy.engine import Connection

import pytest

from kpdesk.billing.schema import BILLING_TABLES, create_billing_schema


class RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: object, parameters: object | None = None) -> None:
        self.statements.append(str(statement))


def test_all_billing_tables_are_forced_rls_and_runtime_has_no_dml() -> None:
    connection = RecordingConnection()
    create_billing_schema(cast(Connection, connection))
    sql = '\n'.join(connection.statements)
    for table in BILLING_TABLES:
        assert f'ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY' in sql
        assert f'REVOKE ALL ON public.{table} FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime' in sql
    assert 'GRANT INSERT' not in sql
    assert "DEFAULT false" in sql
    assert "current_setting('kpdesk.billing_enabled'" not in sql


def test_migration_unapproved_does_not_get_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    path = Path(__file__).parents[1] / 'migrations/versions/0048_lava_billing.py'
    spec = spec_from_file_location('billing_migration', path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.op, 'get_context', lambda: SimpleNamespace(config=SimpleNamespace(attributes={})))
    monkeypatch.setattr(module.op, 'get_bind', lambda: pytest.fail('unapproved migration touched DB'))
    with pytest.raises(RuntimeError, match='explicit approval'):
        module.upgrade()


@pytest.mark.parametrize('role,database,working_approved', [
    ('kpdesk_tenant_runtime', 'kpdesk_accounts_http_abcdef123456', False),
    ('kpdesk', 'kpdesk', False),
    ('kpdesk', 'unmarked_database', True),
])
def test_migration_rejects_wrong_role_unmarked_or_unapproved_working_database(
    monkeypatch: pytest.MonkeyPatch, role: str, database: str, working_approved: bool
) -> None:
    path = Path(__file__).parents[1] / 'migrations/versions/0048_lava_billing.py'
    spec = spec_from_file_location('billing_migration_guard', path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    statements: list[str] = []

    class Target:
        def in_transaction(self) -> bool:
            return True

        def execute(self, statement: object) -> SimpleNamespace:
            statements.append(str(statement))
            return SimpleNamespace(one=lambda: (database, role, module.down_revision))
    config = SimpleNamespace(attributes={
        'kpdesk_lava_billing_0048_approved': True,
        'kpdesk_lava_billing_0048_working_database_approved': working_approved,
    })
    monkeypatch.setattr(module.op, 'get_context', lambda: SimpleNamespace(config=config))
    monkeypatch.setattr(module.op, 'get_bind', Target)
    monkeypatch.setattr(module, 'is_live_accounts_http_clone', lambda connection: database.startswith('kpdesk_accounts_http_'))
    monkeypatch.setattr(module, 'create_billing_schema', lambda connection: pytest.fail('guard executed DDL'))
    with pytest.raises(RuntimeError, match='Unsafe billing migration target'):
        module.upgrade()
    assert all(statement.startswith('SELECT ') for statement in statements)


def test_gateway_acl_keeps_settlement_outside_tenant_role() -> None:
    connection = RecordingConnection()
    create_billing_schema(cast(Connection, connection))
    grants = [sql for sql in connection.statements if sql.startswith('GRANT EXECUTE')]
    assert any('kpdesk_billing_settle(' in sql and sql.endswith('TO kpdesk_auth_runtime') for sql in grants)
    assert not any('kpdesk_billing_settle(' in sql and sql.endswith('TO kpdesk_tenant_runtime') for sql in grants)
    assert not any('kpdesk_billing_configure(' in sql for sql in grants)
    assert not any('kpdesk_billing_entitlement(' in sql for sql in grants)
    definitions = [sql for sql in connection.statements if sql.startswith('CREATE FUNCTION')]
    assert all('SECURITY DEFINER SET search_path=pg_catalog,pg_temp' in sql for sql in definitions)
