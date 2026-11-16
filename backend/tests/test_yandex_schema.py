"""Offline installation/ACL/source guards; SQL execution is a separate rehearsal."""
from importlib import import_module
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest


def schema() -> Any:
    assert (Path(__file__).parents[1] / 'src/kpdesk/yandex/schema.py').exists()
    return import_module('kpdesk.yandex.schema')


def installed_sql() -> Any:
    module = schema()
    conn = Mock()
    module.create_yandex_schema(conn)
    return '\n'.join(str(call.args[0]) for call in conn.execute.call_args_list)


def test_exactly_two_tables_force_rls_and_no_runtime_dml() -> None:
    sql = installed_sql()
    assert sql.count('CREATE TABLE ') == 2
    for table in ('yandex_oauth_flows', 'yandex_identities'):
        assert f'ALTER TABLE public.{table} OWNER TO kpdesk' in sql
        assert f'ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY' in sql
        assert f'REVOKE ALL ON public.{table} FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime' in sql
    assert 'GRANT SELECT' not in sql and 'GRANT INSERT' not in sql
    assert 'UNIQUE(client_id,user_id)' in sql
    assert 'PRIMARY KEY(client_id,subject)' in sql


def test_gateways_have_exact_runtime_authority_and_hardened_path() -> None:
    sql = installed_sql()
    definitions = [part for part in sql.split('CREATE FUNCTION ')[1:]]
    assert len(definitions) == 4
    assert all('SECURITY DEFINER SET search_path=pg_catalog,pg_temp' in part for part in definitions)
    grants = [line for line in sql.splitlines() if line.startswith('GRANT EXECUTE')]
    assert len(grants) == 5
    assert all('kpdesk_yandex_identity(' in line for line in grants if 'TO kpdesk_tenant_runtime' in line)
    assert not any('TO PUBLIC' in line or 'TO kpdesk_app' in line for line in grants)


def test_durable_claim_completion_and_locking_source_guards() -> None:
    sql = installed_sql()
    assert "status='exchanging'" in sql and "status='completed'" in sql
    assert "interval '10 minutes'" in sql and "interval '5 minutes'" in sql
    assert "interval '24 hours'" in sql and 'LIMIT 500' in sql
    assert 'pg_advisory_xact_lock' in sql and 'FOR UPDATE' in sql
    assert 'FOR UPDATE OF u' in sql and 'FOR UPDATE OF s' in sql
    assert 'f.bound_session_hash' in sql and 'f.bound_user_id' in sql
    assert "'Моя компания','active'" in sql
    assert "new_user,p_email,NULL,'active'" in sql
    assert 'INSERT INTO public.auth_sessions' in sql
    assert 'ON CONFLICT DO NOTHING' in sql
    assert 'service_admin' not in sql


def test_readiness_only_reads_catalog_and_checks_acl_policy_ownership() -> None:
    module = schema()
    conn = Mock()
    conn.scalar.return_value = True
    assert module.yandex_available(conn)
    conn.execute.assert_not_called()
    conn.commit.assert_not_called()
    sql = str(conn.scalar.call_args.args[0])
    for check in ('pg_class', 'pg_proc', 'pg_policy', 'relforcerowsecurity',
                  'has_table_privilege', 'has_function_privilege', 'aclexplode', 'proowner'):
        assert check in sql
    assert sql.lstrip().startswith('SELECT')
    assert 'kpdesk_yandex_claim(' not in sql


def test_completion_rechecks_deadline_after_identity_and_user_lock_waits() -> None:
    sql = schema()._COMPLETE
    last_lock = sql.rindex('FOR UPDATE OF u')
    final_expiry_check = sql.rindex('f.expires_at<=observed')
    session_insert = sql.index('INSERT INTO public.auth_sessions')
    assert last_lock < final_expiry_check < session_insert
    assert "WHEN SQLSTATE 'P4901'" in sql


def test_link_issue_checks_captured_session_expiry_after_lock_before_flow_insert() -> None:
    sql = ' '.join(schema()._ISSUE.split())
    capture = 'SELECT s.expires_at INTO bound_session_expires FROM public.auth_sessions s'
    assert capture in sql
    locked_session = sql.index('FOR UPDATE OF s;', sql.index(capture))
    expiry_check = sql.index('bound_session_expires<=clock_timestamp()', locked_session)
    flow_insert = sql.index('INSERT INTO public.yandex_oauth_flows')
    assert locked_session < expiry_check < flow_insert
    assert "RETURN jsonb_build_object('error','session_invalid')" in sql[expiry_check:flow_insert]


def test_link_completion_checks_captured_session_expiry_after_lock_before_binding() -> None:
    sql = ' '.join(schema()._COMPLETE.split())
    capture = 'SELECT s.expires_at INTO bound_session_expires FROM public.auth_sessions s'
    assert capture in sql
    locked_session = sql.index('FOR UPDATE OF s;', sql.index(capture))
    expiry_check = sql.index('bound_session_expires<=clock_timestamp()', locked_session)
    identity_insert = sql.index('INSERT INTO public.yandex_identities', locked_session)
    assert locked_session < expiry_check < identity_insert
    assert "RETURN jsonb_build_object('error','session_invalid')" in sql[expiry_check:identity_insert]


def test_final_original_session_expiry_rolls_back_binding_but_preserves_consumption() -> None:
    sql = ' '.join(schema()._COMPLETE.split())
    consumption = sql.index("SET status='completed',verifier_cipher=NULL")
    provisioning = sql.index('BEGIN SELECT user_id INTO existing_actor')
    final_guard = "IF f.mode='link' AND bound_session_expires<=observed THEN RAISE SQLSTATE 'P4902'; END IF;"
    assert final_guard in sql
    expiry_check = sql.index(final_guard)
    identity_insert = sql.index('INSERT INTO public.yandex_identities')
    fresh_clock = sql.rindex('observed := clock_timestamp();')
    session_insert = sql.index('INSERT INTO public.auth_sessions')
    handler = sql.index("WHEN SQLSTATE 'P4902' THEN RETURN jsonb_build_object('error','session_invalid');")
    assert consumption < provisioning < identity_insert < fresh_clock < expiry_check < session_insert < handler


def migration() -> Any:
    path = Path(__file__).parents[1] / 'migrations/versions/0049_yandex_id.py'
    assert path.exists()
    spec = spec_from_file_location('yandex_migration_test', path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_unapproved_migration_never_obtains_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    module = migration()
    monkeypatch.setattr(module.op, 'get_context', lambda: SimpleNamespace(config=SimpleNamespace(attributes={})))
    monkeypatch.setattr(module.op, 'get_bind', lambda: pytest.fail('unapproved database access'))
    with pytest.raises(RuntimeError, match='explicit approval'):
        module.upgrade()
    with pytest.raises(RuntimeError, match='retention'):
        module.downgrade()


@pytest.mark.parametrize('database,role,previous,working,marked,transaction', [
    ('kpdesk', 'kpdesk', '0048_lava_billing', False, False, True),
    ('unmarked', 'kpdesk', '0048_lava_billing', True, False, True),
    ('clone', 'kpdesk_auth_runtime', '0048_lava_billing', False, True, True),
    ('clone', 'kpdesk', '0047_auth_reset_serialization', False, True, True),
    ('clone', 'kpdesk', '0048_lava_billing', False, True, False),
])
def test_migration_refuses_wrong_target(monkeypatch: pytest.MonkeyPatch, database: Any, role: Any, previous: Any, working: Any, marked: Any, transaction: Any) -> None:
    module = migration()
    conn = Mock()
    conn.in_transaction.return_value = transaction
    conn.execute.return_value.one.return_value = (database, role, previous)
    config = SimpleNamespace(attributes={'kpdesk_yandex_id_0049_approved': True,
        'kpdesk_yandex_id_0049_working_database_approved': working})
    monkeypatch.setattr(module.op, 'get_context', lambda: SimpleNamespace(config=config))
    monkeypatch.setattr(module.op, 'get_bind', lambda: conn)
    monkeypatch.setattr(module, 'is_live_accounts_http_clone', lambda _: marked)
    monkeypatch.setattr(module, 'create_yandex_schema', lambda _: pytest.fail('unapproved DDL'))
    with pytest.raises(RuntimeError, match='Unsafe Yandex migration target'):
        module.upgrade()
    assert all(str(call.args[0]).startswith('SELECT ') for call in conn.execute.call_args_list)
