"""Offline boundaries for the real membership fixture, without SQL/network."""
from collections.abc import Callable, Generator
from inspect import unwrap
from typing import cast
from unittest.mock import MagicMock, Mock
from uuid import UUID

import pytest
import test_tenant_access as tenant
from sqlalchemy import Connection

NAME = "kpdesk_accounts_http_012345abcdef"
MARKER = "kpdesk:accounts-http:012345abcdef"


def membership_fixture() -> Generator[tuple[Connection, dict[str, UUID]]]:
    original = cast(Callable[[], Generator[tuple[Connection, dict[str, UUID]]]], unwrap(tenant.tenant_connection))
    return original()


def fixture_doubles(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock, MagicMock, Mock]:
    context = MagicMock()
    engine = context.__enter__.return_value
    engine.url.database = NAME
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.return_value.one.return_value = (NAME, "kpdesk", "kpdesk", MARKER)
    connection.scalar.return_value = "0005_tenant_expand"
    factory = Mock(return_value=context)
    monkeypatch.setattr(tenant, "legacy_test_database", factory, raising=False)
    forbidden = Mock(side_effect=AssertionError("Fixed-name connection path must never be used"))
    monkeypatch.setattr(tenant, "create_engine", forbidden, raising=False)
    return context, engine, connection, forbidden


def test_membership_fixture_uses_owned_clone_and_closes_on_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    context, engine, connection, forbidden = fixture_doubles(monkeypatch)
    fixture = membership_fixture()
    actual_connection, ids = next(fixture)
    assert actual_connection is connection
    assert "legacy_locked" in ids and "disabled" in ids
    statements = [str(call.args[0]) for call in connection.execute.call_args_list]
    assert "pg_get_userbyid" in statements[0], "Identity guard must run before fixture writes."
    fixture.close()
    forbidden.assert_not_called()
    context.__exit__.assert_called_once()
    connection.begin.return_value.rollback.assert_called_once_with()
    engine.dispose.assert_not_called()  # The lifecycle owns engine disposal.


@pytest.mark.parametrize("identity", [
    ("kpdesk_tenant_test", "kpdesk", "kpdesk", MARKER),
    (NAME, "postgres", "kpdesk", MARKER),
    (NAME, "kpdesk", "postgres", MARKER),
    (NAME, "kpdesk", "kpdesk", None),
    (NAME, "kpdesk", "kpdesk", "kpdesk:accounts-http:ffffffffffff"),
])
def test_membership_fixture_refuses_unverified_target_before_seed(
    monkeypatch: pytest.MonkeyPatch, identity: tuple[object, ...],
) -> None:
    context, _, connection, forbidden = fixture_doubles(monkeypatch)
    connection.execute.return_value.one.return_value = identity
    fixture = membership_fixture()
    with pytest.raises(RuntimeError, match="clone"):
        next(fixture)
    assert all(str(call.args[0]).lstrip().upper().startswith("SELECT")
               for call in connection.execute.call_args_list)
    forbidden.assert_not_called()
    context.__exit__.assert_called_once()


def test_membership_fixture_refuses_unexpected_schema_before_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    context, _, connection, forbidden = fixture_doubles(monkeypatch)
    connection.scalar.return_value = "0042_openrouter_budget"
    fixture = membership_fixture()
    with pytest.raises(RuntimeError, match="schema"):
        next(fixture)
    assert all(str(call.args[0]).lstrip().upper().startswith("SELECT")
               for call in connection.execute.call_args_list)
    forbidden.assert_not_called()
    context.__exit__.assert_called_once()
