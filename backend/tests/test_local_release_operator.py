"""Offline safety contract for the explicit local operator command."""
from dataclasses import replace
from unittest.mock import MagicMock, Mock

import pytest

from scripts import local_release_operator as operator


def source() -> operator.SourceState:
    return operator.SourceState(
        database='kpdesk', role='kpdesk', revision=operator.SOURCE_REVISION,
        server_major=17, counts={**dict.fromkeys(operator.SOURCE_TABLES, 0),
                                 'alembic_version': 1, 'catalog_active': 1},
        inert_catalog_pointer=True, schema_sha256='a' * 64,
        data_sha256='b' * 64, restored_contract_sha256='c' * 64,
    )


def test_default_only_runs_read_only_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    preflight, rehearsal, apply = Mock(return_value=source()), Mock(), Mock()
    monkeypatch.setattr(operator, 'read_source', preflight)
    monkeypatch.setattr(operator, 'rehearse', rehearsal)
    monkeypatch.setattr(operator, 'apply_local', apply)
    assert operator.main([]) == 0
    preflight.assert_called_once_with()
    rehearsal.assert_not_called()
    apply.assert_not_called()


@pytest.mark.parametrize('arguments', [
    ['--apply-local', 'kpdesk:0031-to-0041'],
    ['--apply-local', 'kpdesk:head', '--maintenance-confirmed'],
    ['--maintenance-confirmed'], ['--apply-local', 'kpdesk:0031-to-0041',
                                 '--maintenance-confirmed', '--rehearse'],
])
def test_invalid_mutation_request_stops_before_connecting(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str],
) -> None:
    preflight = Mock()
    monkeypatch.setattr(operator, 'read_source', preflight)
    with pytest.raises(SystemExit):
        operator.main(arguments)
    preflight.assert_not_called()


@pytest.mark.parametrize('field,value', [
    ('database', 'kpdesk_test'), ('role', 'postgres'), ('server_major', 16),
    ('revision', '0041_runtime_readiness'), ('inert_catalog_pointer', False),
])
def test_source_identity_and_schema_must_match(field: str, value: object) -> None:
    with pytest.raises(RuntimeError, match='source'):
        operator.validate_source(replace(source(), **{field: value}))  # type: ignore[arg-type]


@pytest.mark.parametrize('table', ['users', 'organizations', 'account_email_outbox',
                                 'new_unreviewed_table'])
def test_any_nonempty_application_table_blocks_backup(table: str) -> None:
    state = source()
    with pytest.raises(RuntimeError, match='empty'):
        operator.validate_source(replace(state, counts={**state.counts, table: 1}))


def test_normal_source_is_accepted() -> None:
    operator.validate_source(source())


def test_failed_rehearsal_never_reaches_working_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(operator, 'read_source', Mock(return_value=source()))
    monkeypatch.setattr(operator, 'rehearse', Mock(side_effect=RuntimeError('restore failed')))
    upgrade = Mock()
    monkeypatch.setattr(operator, 'upgrade_working', upgrade)
    with pytest.raises(RuntimeError, match='restore failed'):
        operator.apply_local(source(), maintenance_confirmed=True, approval=operator.APPLY_APPROVAL)
    upgrade.assert_not_called()


def test_apply_requires_approval_even_when_called_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    rehearsal = Mock()
    monkeypatch.setattr(operator, 'rehearse', rehearsal)
    with pytest.raises(RuntimeError, match='approval'):
        operator.apply_local(source(), maintenance_confirmed=False, approval=operator.APPLY_APPROVAL)
    rehearsal.assert_not_called()


def test_changed_source_after_rehearsal_blocks_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(operator, 'rehearse', Mock(return_value={'status': 'clone_rehearsal_passed'}))
    monkeypatch.setattr(operator, 'read_source', Mock(return_value=replace(source(), schema_sha256='d' * 64)))
    upgrade = Mock()
    monkeypatch.setattr(operator, 'upgrade_working', upgrade)
    with pytest.raises(RuntimeError, match='changed'):
        operator.apply_local(source(), maintenance_confirmed=True, approval=operator.APPLY_APPROVAL)
    upgrade.assert_not_called()


def test_unaccepted_receipt_never_reaches_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(operator, 'rehearse', Mock(return_value={'status': 'failed'}))
    upgrade = Mock()
    monkeypatch.setattr(operator, 'upgrade_working', upgrade)
    with pytest.raises(RuntimeError, match='fresh backup'):
        operator.apply_local(source(), maintenance_confirmed=True, approval=operator.APPLY_APPROVAL)
    upgrade.assert_not_called()


def test_working_upgrade_uses_ordinary_alembic_and_restores_environment_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('KP_DESK_DB_NAME', 'operator-prior-value')
    monkeypatch.setenv('KP_DESK_APPROVE_LOCAL_0032', 'prior-approval-value')
    monkeypatch.setattr(operator, 'read_source', Mock(return_value=source()))
    monkeypatch.setattr(operator, 'owner_engine', Mock(return_value=MagicMock()))
    observed = []

    def interrupted(config: object, target: str) -> None:
        import os

        from alembic.config import Config

        assert isinstance(config, Config)
        assert 'connection' not in config.attributes
        assert all(config.attributes[name] is True for name in operator.APPROVALS)
        assert target == operator.TARGET_REVISION
        assert os.environ['KP_DESK_DB_NAME'] == 'kpdesk'
        assert os.environ['KP_DESK_APPROVE_LOCAL_0032'] == '1'
        observed.append(True)
        raise RuntimeError('simulated migration failure')

    monkeypatch.setattr('scripts.local_release_operator.command.upgrade', interrupted)
    with pytest.raises(RuntimeError, match='simulated migration failure'):
        operator.upgrade_working(source())
    import os

    assert observed == [True]
    assert os.environ['KP_DESK_DB_NAME'] == 'operator-prior-value'
    assert os.environ['KP_DESK_APPROVE_LOCAL_0032'] == 'prior-approval-value'


def test_rehearsal_cleanup_refuses_unmarked_database() -> None:
    from scripts.local_release_backup import drop_owned_clone

    admin = MagicMock()
    admin.connect.return_value.__enter__.return_value.scalar.return_value = 'somebody-else'
    with pytest.raises(RuntimeError, match='marker'):
        drop_owned_clone(admin, 'kpdesk_accounts_http_012345abcdef')
    admin.connect.return_value.__enter__.return_value.exec_driver_sql.assert_not_called()


@pytest.mark.parametrize('name', ['kpdesk', 'kpdesk_test', 'kpdesk_accounts_http_012345abcdef;DROP'])
def test_cleanup_rejects_unknown_names_before_connection(name: str) -> None:
    from scripts.local_release_backup import drop_owned_clone

    admin = MagicMock()
    with pytest.raises(RuntimeError, match='clone'):
        drop_owned_clone(admin, name)
    admin.connect.assert_not_called()


def test_postgres_restored_string_array_casts_have_same_fingerprint() -> None:
    original = "WHERE ((status)::text = ANY ((ARRAY['pending'::character varying, 'sending'::character varying])::text[]))"
    restored = "WHERE ((status)::text = ANY (ARRAY[('pending'::character varying)::text, ('sending'::character varying)::text]))"
    assert operator.canonical_sql(original) == operator.canonical_sql(restored)
    assert operator.canonical_sql(restored.replace('sending', 'sent')) != operator.canonical_sql(original)
    reordered = original.replace("'pending'::character varying, 'sending'", "'sending'::character varying, 'pending'")
    assert operator.canonical_sql(reordered) != operator.canonical_sql(original)


def test_string_array_normalization_leaves_other_casts_and_escaped_values_intact() -> None:
    unreviewed = "ARRAY[1::integer, 2::integer]::numeric[]"
    assert operator.canonical_sql(unreviewed) == unreviewed
    original = "(ARRAY['don''t'::character varying])::text[]"
    assert operator.canonical_sql(original) == "ARRAY['don''t'::text]"


def test_only_exact_recorded_constraint_parentheses_are_normalized() -> None:
    name = 'tenant_supplier_precision_reviews.ck_precision_review_bounds'
    source_definition = 'CHECK ((((length(TRIM(BOTH FROM supplier_id)) >= 1) AND (length(TRIM(BOTH FROM supplier_id)) <= 120)) AND ((length(TRIM(BOTH FROM rationale)) >= 32) AND (length(TRIM(BOTH FROM rationale)) <= 500))))'
    restored_definition = 'CHECK (((length(TRIM(BOTH FROM supplier_id)) >= 1) AND (length(TRIM(BOTH FROM supplier_id)) <= 120) AND ((length(TRIM(BOTH FROM rationale)) >= 32) AND (length(TRIM(BOTH FROM rationale)) <= 500))))'
    expected = operator.canonical_definition('constraint', name, source_definition)
    assert operator.canonical_definition('constraint', name, restored_definition) == expected
    assert operator.canonical_definition('constraint', name, restored_definition.replace('500', '501')) != expected
    assert operator.canonical_definition('constraint', name, restored_definition.replace(' AND ', ' OR ', 1)) != expected
    assert operator.canonical_definition('constraint', 'unknown', source_definition) != restored_definition


def test_working_failure_is_recorded_without_claiming_rollback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(operator, 'rehearse', Mock(return_value={'status': 'clone_rehearsal_passed'}))
    monkeypatch.setattr(operator, 'read_source', Mock(return_value=source()))
    monkeypatch.setattr(operator, 'upgrade_working', Mock(side_effect=RuntimeError('migration failed')))
    saved = Mock()
    monkeypatch.setattr(operator, 'write_apply_evidence', saved)
    with pytest.raises(RuntimeError, match='migration failed'):
        operator.apply_local(source(), maintenance_confirmed=True, approval=operator.APPLY_APPROVAL)
    assert saved.call_args.args[1]['status'] == 'failed_requires_operator_review'
    assert saved.call_args.args[1]['automatic_rollback'] is False


def test_unreviewed_empty_table_is_rejected_before_backup() -> None:
    state = source()
    with pytest.raises(RuntimeError, match='inventory'):
        operator.validate_source(replace(state, counts={**state.counts, 'unknown_empty_table': 0}))


def test_schema_outside_public_is_rejected_before_backup() -> None:
    with pytest.raises(RuntimeError, match='schema'):
        operator.validate_source(replace(source(), unexpected_schemas=('customer_data',)))


def test_security_definer_owner_cannot_silently_change_on_restore() -> None:
    with pytest.raises(RuntimeError, match='owner'):
        operator.validate_source(replace(source(), object_owners=('kpdesk', 'different_security_owner')))


@pytest.mark.parametrize('permissions', [(False, False), (True, True), (None, False)])
def test_auth_runtime_requires_gateway_and_forbids_direct_table_select(permissions: tuple[object, object]) -> None:
    from scripts.local_release_backup import verify_auth_contract

    connection = MagicMock()
    connection.execute.return_value.one.return_value = permissions
    with pytest.raises(RuntimeError, match='Auth runtime'):
        verify_auth_contract(connection)


def test_expected_auth_runtime_permission_contract_passes() -> None:
    from scripts.local_release_backup import verify_auth_contract

    connection = MagicMock()
    connection.execute.return_value.one.return_value = (True, False)
    verify_auth_contract(connection)


def test_database_acl_replay_refuses_unmarked_clone_before_any_grants() -> None:
    from scripts.local_release_backup import restore_clone_database_acl

    owner = MagicMock()
    owner.begin.return_value.__enter__.return_value.scalar.return_value = 'kpdesk'
    with pytest.raises(RuntimeError, match='marked clone'):
        restore_clone_database_acl(owner, 'kpdesk_accounts_http_012345abcdef', source())
    owner.begin.return_value.__enter__.return_value.exec_driver_sql.assert_not_called()
