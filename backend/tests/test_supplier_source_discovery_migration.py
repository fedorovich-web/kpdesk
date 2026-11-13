"""0035 is explicitly gated and exposes only bounded tenant-attested source refs."""
from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

migration = importlib.import_module("migrations.versions.0035_supplier_source_discovery")


@pytest.mark.parametrize("action", ("upgrade", "downgrade"))
def test_no_explicit_approval_no_database_access(
    monkeypatch: pytest.MonkeyPatch, action: str,
) -> None:
    conn = Mock()
    monkeypatch.setattr(migration.op, "get_context", lambda: SimpleNamespace(
        config=SimpleNamespace(attributes={}),
    ))
    monkeypatch.setattr(migration.op, "get_bind", lambda: conn)
    with pytest.raises(RuntimeError, match="explicit Alembic approval"):
        getattr(migration, action)()
    conn.execute.assert_not_called()


@pytest.mark.parametrize(("name", "role", "revision"), [
    ("bad_db", "kpdesk", "0034_reviewed_source_bridge"),
    ("kpdesk", "kpdesk_tenant_runtime", "0034_reviewed_source_bridge"),
    ("kpdesk", "kpdesk", "0033_reviewed_catalog_provenance"),
])
def test_bad_database_owner_or_revision_no_ddl(
    monkeypatch: pytest.MonkeyPatch, name: str, role: str, revision: str,
) -> None:
    conn = Mock()
    conn.in_transaction.return_value = True
    conn.execute.return_value.one.return_value = (name, role, revision)
    monkeypatch.setattr(migration.op, "get_context", lambda: SimpleNamespace(
        config=SimpleNamespace(attributes={
            "kpdesk_supplier_discovery_0035_approved": True,
        }),
    ))
    monkeypatch.setattr(migration.op, "get_bind", lambda: conn)
    monkeypatch.setattr(migration, "is_live_accounts_http_clone", lambda _: False)
    with pytest.raises(RuntimeError, match="target is unsafe"):
        migration.upgrade()
    assert conn.execute.call_count == 1


def test_sql_stays_read_only_and_auth_and_pagination_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = Mock()
    conn.scalar.return_value = None
    monkeypatch.setattr(migration, "_preflight", lambda _: conn)
    migration.upgrade()
    executed = "\n".join(str(item.args[0]) for item in conn.execute.call_args_list)
    assert "SECURITY DEFINER" in executed
    assert "p_limit BETWEEN 1 AND 100" in executed
    assert "p_offset BETWEEN 0 AND 10000" in executed
    assert "kpdesk_session_tenant_allowed(p_org,'read')" in executed
    assert "v.source_sha256=p.canonical_csv_sha256" in executed
    assert "GRANT EXECUTE ON FUNCTION" in executed
    assert "TO kpdesk_tenant_runtime" in executed
    assert "GRANT SELECT" not in executed
    assert "INSERT INTO" not in executed
