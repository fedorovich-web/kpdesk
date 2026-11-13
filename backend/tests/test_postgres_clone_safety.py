"""Offline safety regressions for the legacy SQL test lifecycle."""
from unittest.mock import MagicMock, Mock

import postgres_clone_connection as clones
import pytest

NAME = "kpdesk_accounts_http_012345abcdef"
MARKER = "kpdesk:accounts-http:012345abcdef"


@pytest.mark.parametrize("name", ["kpdesk", "kpdesk_test", "postgres", NAME + ";DROP", NAME.upper()])
def test_clone_guard_rejects_unknown_names_before_sql(name: str) -> None:
    connection = MagicMock()
    with pytest.raises(RuntimeError, match="clone"):
        clones.require_legacy_test_clone(connection, name)
    connection.execute.assert_not_called()


@pytest.mark.parametrize("row", [
    ("kpdesk", "kpdesk", "kpdesk", MARKER),
    (NAME, "postgres", "kpdesk", MARKER),
    (NAME, "kpdesk", "postgres", MARKER),
    (NAME, "kpdesk", "kpdesk", None),
    (NAME, "kpdesk", "kpdesk", "kpdesk:accounts-http:ffffffffffff"),
])
def test_clone_guard_rejects_identity_owner_or_marker_mismatch(row: tuple[object, ...]) -> None:
    connection = MagicMock()
    connection.execute.return_value.one.return_value = row
    with pytest.raises(RuntimeError, match="clone"):
        clones.require_legacy_test_clone(connection, NAME)
    connection.exec_driver_sql.assert_not_called()
    assert all(str(call.args[0]).lstrip().upper().startswith("SELECT")
               for call in connection.execute.call_args_list)


def test_clone_guard_accepts_exact_identity_and_marker() -> None:
    connection = MagicMock()
    connection.execute.return_value.one.return_value = (NAME, "kpdesk", "kpdesk", MARKER)
    clones.require_legacy_test_clone(connection, NAME)
    connection.exec_driver_sql.assert_not_called()


def test_legacy_lifecycle_requires_opt_in_before_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(clones.LEGACY_TEST_OPT_IN, raising=False)
    factory = Mock()
    monkeypatch.setattr(clones, "postgres_test_engine", factory)
    with pytest.raises(RuntimeError, match="opt-in"), clones.legacy_test_database():
        pytest.fail("Unapproved SQL lifecycle yielded an engine")
    factory.assert_not_called()


@pytest.mark.parametrize("row", [
    ("kpdesk", "kpdesk", "kpdesk", MARKER),
    ("postgres", "postgres", "kpdesk", MARKER),
    ("postgres", "kpdesk", "postgres", MARKER),
    ("postgres", "kpdesk", "kpdesk", None),
    ("postgres", "kpdesk", "kpdesk", "kpdesk:accounts-http:ffffffffffff"),
])
def test_cleanup_refuses_unidentified_target(row: tuple[object, ...]) -> None:
    admin = MagicMock()
    connection = admin.connect.return_value.__enter__.return_value
    connection.execute.return_value.one.return_value = row
    with pytest.raises(RuntimeError, match="clone"):
        clones.drop_legacy_test_clone(admin, NAME)
    connection.exec_driver_sql.assert_not_called()


def test_cleanup_drops_only_exact_marked_clone() -> None:
    admin = MagicMock()
    connection = admin.connect.return_value.__enter__.return_value
    connection.execute.return_value.one.return_value = ("postgres", "kpdesk", "kpdesk", MARKER)
    clones.drop_legacy_test_clone(admin, NAME)
    connection.exec_driver_sql.assert_called_once_with(f'DROP DATABASE "{NAME}" WITH (FORCE)')


def test_cleanup_rejects_fixed_database_before_connection() -> None:
    admin = MagicMock()
    with pytest.raises(RuntimeError, match="clone"):
        clones.drop_legacy_test_clone(admin, "kpdesk_test")
    admin.connect.assert_not_called()


def test_invalid_generated_name_stops_before_connecting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(clones.LEGACY_TEST_OPT_IN, "1")
    monkeypatch.setattr("postgres_clone_connection.secrets.token_hex", lambda count: "not_a_nonce")
    factory = Mock()
    monkeypatch.setattr(clones, "postgres_test_engine", factory)
    with pytest.raises(RuntimeError, match="clone"), clones.legacy_test_database():
        pytest.fail("Unsafe generated name accepted")
    factory.assert_not_called()


@pytest.mark.parametrize("failure_stage", ["none", "identity", "migration", "body"])
def test_lifecycle_checks_before_migration_and_always_guards_cleanup(
    monkeypatch: pytest.MonkeyPatch, failure_stage: str,
) -> None:
    monkeypatch.setenv(clones.LEGACY_TEST_OPT_IN, "1")
    monkeypatch.setattr("postgres_clone_connection.secrets.token_hex", lambda count: "012345abcdef")
    admin, engine = MagicMock(), MagicMock()
    connection = admin.connect.return_value.__enter__.return_value
    connection.execute.return_value.one.return_value = ("postgres", "kpdesk")
    monkeypatch.setattr(clones, "postgres_test_engine", Mock(side_effect=[admin, engine]))
    events: list[str] = []

    def check(*args: object) -> None:
        events.append("check")
        if failure_stage == "identity":
            raise RuntimeError("clone identity rejected")

    def migrate(*args: object) -> None:
        events.append("migrate")
        if failure_stage == "migration":
            raise RuntimeError("migration failed")

    monkeypatch.setattr(clones, "require_legacy_test_clone", check)
    monkeypatch.setattr(clones, "_migrate_legacy_database", migrate)
    cleanup = Mock()
    monkeypatch.setattr(clones, "drop_legacy_test_clone", cleanup)

    def exercise() -> None:
        with clones.legacy_test_database() as actual:
            assert actual is engine and events == ["check", "migrate", "check"]
            events.append("body")
            if failure_stage == "body":
                raise RuntimeError("body failed")

    if failure_stage == "none":
        exercise()
    else:
        with pytest.raises(RuntimeError):
            exercise()
    if failure_stage == "identity":
        assert events == ["check"]
    cleanup.assert_called_once_with(admin, NAME)
    engine.dispose.assert_called_once_with()
    admin.dispose.assert_called_once_with()


def test_lifecycle_refuses_non_admin_connection_before_create(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(clones.LEGACY_TEST_OPT_IN, "1")
    admin = MagicMock()
    connection = admin.connect.return_value.__enter__.return_value
    connection.execute.return_value.one.return_value = ("kpdesk", "kpdesk")
    monkeypatch.setattr(clones, "postgres_test_engine", Mock(return_value=admin))
    with pytest.raises(RuntimeError, match="admin"), clones.legacy_test_database():
        pytest.fail("Unexpected lifecycle entry")
    connection.exec_driver_sql.assert_not_called()
