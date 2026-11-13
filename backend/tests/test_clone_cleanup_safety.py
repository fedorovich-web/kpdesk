"""Execute actual harness finalizers offline with guarded fake admin metadata."""
import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from postgres_clone_connection import drop_legacy_test_clone
from sqlalchemy import text

CASES = [
    ("test_reviewed_catalog_provenance_postgres.py", "test_0033_provenance_is_installed_inactive_and_reversible_on_empty_clone", True),
    ("test_supplier_mapping_history_postgres.py", "test_a26_history_is_session_bound_tenant_scoped_and_paginated", True),
    ("test_supplier_precision_postgres.py", "test_a22_precision_reviews_postgresql_clone_only", True),
    ("test_supplier_unit_postgres.py", "test_a31_unit_review_real_postgres_restrictions_and_idempotency", True),
    ("test_tenant_candidate_http.py", "test_candidate_sql_audit_and_https_with_actual_tenant_session", True),
    ("test_a45_live_role_browser.py", "test_https_real_auth_and_tenant_logins_with_manager_audit", True),
    ("canonical_release_runtime_acceptance.py", "verify_release_runtime", False),
]
NAME = "kpdesk_accounts_http_012345abcdef"
MARKER = "kpdesk:accounts-http:012345abcdef"


def run_finalizer(filename: str, function_name: str, admin: MagicMock, *, created: bool = True) -> None:
    source = Path(__file__).with_name(filename).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=filename)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == function_name)
    finalizer = [node for node in function.body if isinstance(node, ast.Try) and node.finalbody][-1]
    module = ast.Module(body=finalizer.finalbody, type_ignores=[])
    # Only the existing finally block runs: no body, imports, database setup,
    # subprocesses, HTTP dispatch or browser. Metadata reads use a fake admin.
    exec(compile(module, filename, "exec"), {  # noqa: S102 - trusted local finalizer AST; fake globals only
        "admin": admin, "name": NAME, "nonce": NAME.rsplit("_", 1)[-1],
        "created": created, "engines": [], "text": text,
        "drop_test_clone": drop_legacy_test_clone,
    })


@pytest.mark.parametrize("filename,function_name,owns_admin", CASES)
@pytest.mark.parametrize("identity", [
    ("kpdesk", "kpdesk", "kpdesk", MARKER),
    ("postgres", "postgres", "kpdesk", MARKER),
    ("postgres", "kpdesk", "postgres", MARKER),
    ("postgres", "kpdesk", "kpdesk", None),
    ("postgres", "kpdesk", "kpdesk", "kpdesk:accounts-http:ffffffffffff"),
])
def test_actual_cleanup_refuses_wrong_admin_owner_or_marker(
    filename: str, function_name: str, owns_admin: bool, identity: tuple[object, ...],
) -> None:
    admin = MagicMock()
    connection = admin.connect.return_value.__enter__.return_value
    connection.execute.return_value.one.return_value = identity
    connection.scalar.return_value = MARKER
    with pytest.raises(RuntimeError, match="clone"):
        run_finalizer(filename, function_name, admin)
    connection.exec_driver_sql.assert_not_called()
    if owns_admin:
        admin.dispose.assert_called_once_with()
    else:
        admin.dispose.assert_not_called()


@pytest.mark.parametrize("filename,function_name,owns_admin", CASES)
def test_actual_cleanup_drops_verified_clone_only(
    filename: str, function_name: str, owns_admin: bool,
) -> None:
    admin = MagicMock()
    connection = admin.connect.return_value.__enter__.return_value
    connection.execute.return_value.one.return_value = ("postgres", "kpdesk", "kpdesk", MARKER)
    run_finalizer(filename, function_name, admin)
    connection.exec_driver_sql.assert_called_once_with(f'DROP DATABASE "{NAME}" WITH (FORCE)')
    if owns_admin:
        admin.dispose.assert_called_once_with()


@pytest.mark.parametrize("filename,function_name,owns_admin", CASES)
def test_actual_cleanup_without_created_target_never_opens_admin_connection(
    filename: str, function_name: str, owns_admin: bool,
) -> None:
    admin = MagicMock()
    run_finalizer(filename, function_name, admin, created=False)
    admin.connect.assert_not_called()
    if owns_admin:
        admin.dispose.assert_called_once_with()
