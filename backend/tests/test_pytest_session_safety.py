"""Session-wide guards: fake DBAPI only, no credentials or physical SQL."""
from unittest.mock import MagicMock, Mock

import psycopg
import pytest
from psycopg.rows import tuple_row
from sqlalchemy import create_engine as make_engine
from sqlalchemy.engine import URL

from scripts import pytest_safety as safety

NAME = "kpdesk_accounts_http_012345abcdef"
MARKER = "kpdesk:accounts-http:012345abcdef"
PARAMS = {"dbname": NAME, "user": "kpdesk", "host": "127.0.0.1", "port": "55432"}


def policy(*, enabled: bool = True) -> tuple[safety.SessionSafety, Mock, MagicMock]:
    dbapi = MagicMock()
    dbapi.cursor.return_value.__enter__.return_value.fetchone.return_value = (NAME, "kpdesk", "kpdesk", MARKER)
    connector = Mock(return_value=dbapi)
    return safety.SessionSafety(allow_postgres=enabled, connector=connector), connector, dbapi


@pytest.mark.parametrize("name", ["kpdesk", "kpdesk_test", "kpdesk_tenant_test", "postgres", NAME])
def test_offline_connection_is_denied_before_driver(name: str) -> None:
    guard, connector, _ = policy(enabled=False)
    with pytest.raises(safety.SafetyViolation):
        guard.connect(**{**PARAMS, "dbname": name})
    connector.assert_not_called()


@pytest.mark.parametrize("override", [
    {"dbname": "kpdesk"}, {"dbname": "kpdesk_test"}, {"dbname": "kpdesk_tenant_test"},
    {"dbname": NAME + ";DROP"}, {"host": "example.invalid"}, {"host": ""},
    {"port": "5432"}, {"user": "postgres"}, {"options": "-c role=postgres"},
    {"service": "unsafe"}, {"hostaddr": "198.51.100.1"},
])
def test_invalid_target_or_connection_options_never_reach_driver(override: dict[str, str]) -> None:
    guard, connector, _ = policy()
    guard.owned.add(NAME)
    guard.marked.add(NAME)
    with pytest.raises(safety.SafetyViolation):
        guard.connect(**{**PARAMS, **override})
    connector.assert_not_called()


def test_preexisting_well_named_clone_is_not_a_session_owned_target() -> None:
    guard, connector, _ = policy()
    with pytest.raises(safety.SafetyViolation):
        guard.connect(**PARAMS)
    connector.assert_not_called()


def test_clone_connection_requires_successful_comment() -> None:
    guard, connector, _ = policy()
    guard.owned.add(NAME)
    with pytest.raises(safety.SafetyViolation):
        guard.connect(**PARAMS)
    connector.assert_not_called()


@pytest.mark.parametrize("identity", [
    ("kpdesk", "kpdesk", "kpdesk", MARKER), (NAME, "postgres", "kpdesk", MARKER),
    (NAME, "kpdesk", "postgres", MARKER), (NAME, "kpdesk", "kpdesk", None),
    (NAME, "kpdesk", "kpdesk", "wrong"), None,
])
def test_actual_driver_identity_mismatch_closes_connection(identity: object) -> None:
    guard, _, dbapi = policy()
    guard.owned.add(NAME)
    guard.marked.add(NAME)
    dbapi.cursor.return_value.__enter__.return_value.fetchone.return_value = identity
    with pytest.raises(safety.SafetyViolation):
        guard.connect(**PARAMS)
    dbapi.close.assert_called_once_with()


def test_verified_owned_clone_connects_and_metadata_transaction_is_rolled_back() -> None:
    guard, connector, dbapi = policy()
    guard.owned.add(NAME)
    guard.marked.add(NAME)
    assert guard.connect(**PARAMS) is dbapi
    connector.assert_called_once()
    dbapi.rollback.assert_called_once_with()


def test_admin_connection_requires_actual_local_owner_identity() -> None:
    guard, _, dbapi = policy()
    dbapi.cursor.return_value.__enter__.return_value.fetchone.return_value = ("postgres", "kpdesk", "postgres", None)
    assert guard.connect(**{**PARAMS, "dbname": "postgres"}) is dbapi


def test_sqlalchemy_integer_port_keeps_the_same_loopback_boundary() -> None:
    guard, _, dbapi = policy()
    guard.owned.add(NAME)
    guard.marked.add(NAME)
    assert guard.connect(dbname=NAME, user="kpdesk", host="127.0.0.1", port=55432) is dbapi


def test_identity_query_uses_tuple_rows_and_restores_caller_row_factory() -> None:
    guard, _, _ = policy()
    cursor = MagicMock()
    previous = cursor.row_factory

    def fetch() -> tuple[object, ...]:
        assert cursor.row_factory is tuple_row
        return ("postgres", "kpdesk", "postgres", None)

    cursor.fetchone.side_effect = fetch
    guard._identity(cursor, "postgres")
    assert cursor.row_factory is previous


def admin_doubles() -> tuple[MagicMock, MagicMock]:
    connection, cursor = MagicMock(), MagicMock()
    connection.engine.url.database = "postgres"
    cursor.fetchone.return_value = ("postgres", "kpdesk", "postgres", None)
    return connection, cursor


def execute(guard: safety.SessionSafety, connection: MagicMock, cursor: MagicMock, sql: str) -> None:
    guard.before_execute(connection, cursor, sql, {}, None, False)


@pytest.mark.parametrize("sql", [
    "DROP DATABASE kpdesk", f'DROP DATABASE "{NAME}" WITH (FORCE)',
    f"COMMENT ON DATABASE \"{NAME}\" IS '{MARKER}'", "CREATE TABLE accidental(id int)",
    "SELECT pg_terminate_backend(1)", "SELECT 1; DROP DATABASE kpdesk", "SET ROLE postgres",
])
def test_admin_cannot_mutate_arbitrary_or_unowned_targets(sql: str) -> None:
    guard, _, _ = policy()
    connection, cursor = admin_doubles()
    with pytest.raises(safety.SafetyViolation):
        execute(guard, connection, cursor, sql)


def test_driver_type_metadata_query_is_read_only_and_exact() -> None:
    guard, _, _ = policy()
    connection, cursor = admin_doubles()
    statement = """SELECT typname AS name, oid, typarray AS array_oid,
        oid::regtype::text AS regtype, typdelim AS delimiter
        FROM pg_type t WHERE t.oid = to_regtype(%(name)s) ORDER BY t.oid"""
    execute(guard, connection, cursor, statement)
    with pytest.raises(safety.SafetyViolation):
        execute(guard, connection, cursor, statement.replace("typname AS name", "pg_terminate_backend(1) AS name"))


def test_owned_lifecycle_is_recorded_only_after_successful_commands() -> None:
    guard, _, _ = policy()
    connection, cursor = admin_doubles()
    create = f'CREATE DATABASE "{NAME}" TEMPLATE template0 OWNER kpdesk'
    execute(guard, connection, cursor, create)
    assert NAME not in guard.owned
    guard.after_execute(connection, cursor, create, {}, None, False)
    assert NAME in guard.owned and NAME not in guard.marked
    comment = f"COMMENT ON DATABASE \"{NAME}\" IS '{MARKER}'"
    cursor.fetchone.side_effect = [("postgres", "kpdesk", "postgres", None), ("postgres", "kpdesk", "kpdesk", None)]
    execute(guard, connection, cursor, comment)
    guard.after_execute(connection, cursor, comment, {}, None, False)
    assert NAME in guard.marked
    drop = f'DROP DATABASE "{NAME}" WITH (FORCE)'
    cursor.fetchone.side_effect = [("postgres", "kpdesk", "postgres", None), ("postgres", "kpdesk", "kpdesk", MARKER)]
    execute(guard, connection, cursor, drop)
    guard.after_execute(connection, cursor, drop, {}, None, False)
    assert not guard.owned and not guard.marked


@pytest.mark.parametrize("identity", [
    ("postgres", "kpdesk", "postgres", MARKER),
    ("postgres", "kpdesk", "kpdesk", None),
    ("postgres", "kpdesk", "kpdesk", "wrong"),
])
def test_drop_rechecks_target_owner_and_marker_even_if_caller_skips_its_guard(identity: object) -> None:
    guard, _, _ = policy()
    guard.owned.add(NAME)
    guard.marked.add(NAME)
    connection, cursor = admin_doubles()
    cursor.fetchone.side_effect = [("postgres", "kpdesk", "postgres", None), identity]
    with pytest.raises(safety.SafetyViolation):
        execute(guard, connection, cursor, f'DROP DATABASE "{NAME}" WITH (FORCE)')
    assert NAME in guard.owned


def test_clone_sql_rechecks_marker_on_reused_connection() -> None:
    guard, _, _ = policy()
    guard.owned.add(NAME)
    guard.marked.add(NAME)
    connection, cursor = admin_doubles()
    connection.engine.url.database = NAME
    cursor.fetchone.return_value = (NAME, "kpdesk", "kpdesk", "wrong")
    with pytest.raises(safety.SafetyViolation):
        execute(guard, connection, cursor, "INSERT INTO synthetic VALUES (1)")


def test_errors_never_echo_password_or_connection_string() -> None:
    guard, connector, _ = policy()
    with pytest.raises(safety.SafetyViolation) as error:
        guard.connect("dbname=kpdesk user=kpdesk password=never-show-this host=example.invalid")
    assert "never-show-this" not in str(error.value)
    connector.assert_not_called()


@pytest.mark.parametrize("name", ["kpdesk", "kpdesk_test", "kpdesk_tenant_test"])
def test_raw_driver_cursor_cannot_use_a_preexisting_fixed_database(name: str) -> None:
    guard, _, _ = policy()
    guard._execute = Mock()
    cursor = MagicMock()
    cursor.connection.info.dbname = name
    with pytest.raises(safety.SafetyViolation):
        guard.execute_raw(cursor, "DELETE FROM synthetic")
    guard._execute.assert_not_called()


def test_raw_driver_admin_cannot_bypass_drop_guard() -> None:
    guard, _, _ = policy()
    guard._execute = Mock()
    cursor = MagicMock()
    cursor.connection.info.dbname = "postgres"
    cursor.fetchone.return_value = ("postgres", "kpdesk", "postgres", None)
    with pytest.raises(safety.SafetyViolation):
        guard.execute_raw(cursor, "DROP DATABASE kpdesk")
    guard._execute.assert_not_called()


def test_raw_driver_create_is_tracked_after_success() -> None:
    guard, _, _ = policy()
    guard._execute = Mock()
    cursor = MagicMock()
    cursor.connection.info.dbname = "postgres"
    cursor.fetchone.return_value = ("postgres", "kpdesk", "postgres", None)
    guard.execute_raw(cursor, f'CREATE DATABASE "{NAME}" TEMPLATE template0 OWNER kpdesk')
    assert NAME in guard.owned
    guard._execute.assert_called_once()


def test_bulk_admin_commands_are_not_an_escape_hatch() -> None:
    guard, _, _ = policy()
    guard._executemany = Mock()
    cursor = MagicMock()
    cursor.connection.info.dbname = "postgres"
    with pytest.raises(safety.SafetyViolation):
        guard.executemany_raw(cursor, "DROP DATABASE kpdesk", [()])
    guard._executemany.assert_not_called()


def test_uppercase_clone_name_is_rejected_before_create() -> None:
    guard, _, _ = policy()
    connection, cursor = admin_doubles()
    with pytest.raises(safety.SafetyViolation):
        execute(guard, connection, cursor, f'CREATE DATABASE "{NAME.upper()}" TEMPLATE template0 OWNER kpdesk')


@pytest.mark.parametrize("name", ["kpdesk", "kpdesk_test", "kpdesk_tenant_test", "postgres", NAME])
def test_actual_pytest_hook_blocks_driver_before_a_physical_connection(
    monkeypatch: pytest.MonkeyPatch, name: str,
) -> None:
    driver = Mock(side_effect=AssertionError("Physical driver must never be reached"))
    monkeypatch.setattr(psycopg, "connect", driver)
    engine = make_engine(URL.create("postgresql+psycopg", username="kpdesk", password="synthetic-only",
                                    host="127.0.0.1", port=55432, database=name))
    try:
        with pytest.raises(safety.SafetyViolation):
            engine.connect()
        driver.assert_not_called()
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_async_driver_is_not_an_alternate_sql_profile() -> None:
    with pytest.raises(safety.SafetyViolation):
        await psycopg.AsyncConnection.connect("dbname=kpdesk user=kpdesk host=127.0.0.1")


def test_public_connection_class_cannot_bypass_offline_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = Mock(side_effect=AssertionError("Physical driver must never be reached"))
    monkeypatch.setattr(psycopg.Connection, "connect", driver)
    guard, _, _ = policy(enabled=False)
    try:
        guard.start()
        with pytest.raises(safety.SafetyViolation):
            psycopg.Connection.connect("dbname=kpdesk host=example.invalid")
        driver.assert_not_called()
    finally:
        guard.close()


@pytest.mark.parametrize("key", ["PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS", "PGPASSFILE"])
def test_ambient_libpq_overrides_are_refused_before_driver(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    guard, connector, _ = policy()
    monkeypatch.setenv(key, "synthetic-override")
    with pytest.raises(safety.SafetyViolation):
        guard.connect(**{**PARAMS, "dbname": "postgres"})
    connector.assert_not_called()


@pytest.mark.parametrize("method", ["connect_ex", "sendto"])
def test_alternate_socket_paths_are_blocked(method: str) -> None:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock, pytest.raises(safety.SafetyViolation):
        if method == "connect_ex":
            sock.connect_ex(("127.0.0.1", 9))
        else:
            sock.sendto(b"synthetic", ("127.0.0.1", 9))


@pytest.mark.parametrize("sql", [
    "CREATE ROLE unexpected", "ALTER USER kpdesk SUPERUSER", "DROP GROUP unexpected",
    "ALTER SYSTEM SET max_connections = 200", "CREATE TABLESPACE unexpected LOCATION '/tmp'",
    "DROP DATABASE kpdesk", "GRANT kpdesk TO unexpected", "REVOKE kpdesk FROM unexpected",
    "SELECT pg_terminate_backend(1)", "SELECT 1; /* marker */ DROP ROLE unexpected",
])
def test_owned_clone_cannot_mutate_cluster_resources(sql: str) -> None:
    guard, _, _ = policy()
    guard.owned.add(NAME)
    guard.marked.add(NAME)
    connection, cursor = admin_doubles()
    connection.engine.url.database = NAME
    cursor.fetchone.return_value = (NAME, "kpdesk", "kpdesk", MARKER)
    with pytest.raises(safety.SafetyViolation):
        execute(guard, connection, cursor, sql)


def test_failed_clone_transaction_can_roll_back_to_its_savepoint_without_metadata_query() -> None:
    guard, _, _ = policy()
    guard.owned.add(NAME)
    guard.marked.add(NAME)
    connection, cursor = admin_doubles()
    connection.engine.url.database = NAME
    cursor.connection.info.dbname = NAME
    cursor.connection.info.user = "kpdesk"
    cursor.execute.side_effect = AssertionError("Aborted transactions cannot execute metadata SELECT")
    execute(guard, connection, cursor, "ROLLBACK TO SAVEPOINT sa_savepoint_1")
    cursor.execute.assert_not_called()


def test_savepoint_recovery_still_requires_the_actual_owned_connection() -> None:
    guard, _, _ = policy()
    guard.owned.add(NAME)
    guard.marked.add(NAME)
    connection, cursor = admin_doubles()
    connection.engine.url.database = NAME
    cursor.connection.info.dbname = "kpdesk"
    cursor.connection.info.user = "kpdesk"
    with pytest.raises(safety.SafetyViolation):
        execute(guard, connection, cursor, "ROLLBACK TO SAVEPOINT sa_savepoint_1")
