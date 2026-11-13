"""Security regression: one SQL metadata inspection, unchanged fail-closed scopes.

No PostgreSQL fixture is required here; the canonical A45 clone and A8 tests
exercise the real SQL and deny cross-tenant access in integration.
"""
from __future__ import annotations

from typing import cast
from unittest.mock import Mock

import pytest
from sqlalchemy import Connection

from kpdesk.db.auth_sessions import _test_schema_guard
from kpdesk.db.live_rehearsal_scope import authenticated_database_environment


def _database(
    name: str, role: str, marker: str | None,
    *, gateway: bool = True, tenant_rls: bool = True,
) -> Connection:
    connection = Mock(spec=Connection)
    connection.in_transaction.return_value = True
    connection.execute.return_value.one.return_value = (
        name, role, gateway, tenant_rls, False, marker,
    )
    return cast(Connection, connection)


def test_metadata_is_one_round_trip_on_valid_clone() -> None:
    name = "kpdesk_accounts_http_a123fabc0098"
    connection = _database(
        name, "kpdesk_tenant_runtime", "kpdesk:accounts-http:a123fabc0098",
    )
    metadata = authenticated_database_environment(connection)
    assert metadata.is_marked_clone is True
    assert metadata.database == name
    assert metadata.role == "kpdesk_tenant_runtime"
    assert metadata.tenant_rls_available is True
    cast(Mock, connection.execute).assert_called_once()


@pytest.mark.parametrize("name,marker", [
    ("kpdesk_accounts_http_a123fabc0098", None),
    ("kpdesk_accounts_http_a123fabc0098", "kpdesk:accounts-http:000000000000"),
    ("kpdesk_accounts_http_A123FABC0098", "kpdesk:accounts-http:A123FABC0098"),
    ("kpdesk_accounts_http_a123fabc0098_backdoor", "kpdesk:accounts-http:a123fabc0098_backdoor"),
    ("kpdesk", "kpdesk:accounts-http:a123fabc0098"),
])
def test_forged_database_clone_identity_not_accepted(name: str, marker: str | None) -> None:
    environment = authenticated_database_environment(
        _database(name, "kpdesk_tenant_runtime", marker),
    )
    assert environment.is_marked_clone is False


@pytest.mark.parametrize("name,role,gateway,rls", [
    ("kpdesk_accounts_http_a123fabc0098", "kpdesk", True, True),
    ("kpdesk_accounts_http_a123fabc0098", "kpdesk_app", True, True),
    ("kpdesk_accounts_http_a123fabc0098", "kpdesk_tenant_runtime", False, True),
    ("kpdesk_accounts_http_a123fabc0098", "kpdesk_tenant_runtime", True, False),
    ("kpdesk", "kpdesk", True, True),
])
def test_auth_gateway_role_and_function_guards_still_fail_closed(
    name: str, role: str, gateway: bool, rls: bool,
) -> None:
    marker = (
        "kpdesk:accounts-http:a123fabc0098"
        if name != "kpdesk" else None
    )
    connection = _database(
        name, role, marker, gateway=gateway, tenant_rls=rls,
    )
    with pytest.raises(RuntimeError, match="role|gateway|RLS|allowed"):
        _test_schema_guard(connection, "validate")


def test_auth_test_role_cloning_marker_absent_fails() -> None:
    connection = _database(
        "kpdesk_accounts_http_a123fabc0098",
        "kpdesk_tenant_runtime",
        "kpdesk:accounts-http:bogus",
    )
    with pytest.raises(RuntimeError, match="Unrecognized"):
        _test_schema_guard(connection, "validate")
