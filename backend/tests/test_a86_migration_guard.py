"""A86 Alembic review migration must refuse non-clone targets before DDL."""
from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import Mock

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory


@pytest.mark.parametrize("action", ["upgrade", "downgrade"])
@pytest.mark.parametrize(
    ("in_transaction", "marked_clone", "owner"),
    [
        (False, True, "kpdesk"),
        (True, False, "kpdesk"),
        (True, True, "kpdesk_tenant_runtime"),
    ],
)
def test_clone_migration_refuses_unsafe_target(
    monkeypatch: pytest.MonkeyPatch, action: str,
    in_transaction: bool, marked_clone: bool, owner: str,
) -> None:
    migration = importlib.import_module(
        "migrations.clone_versions.reviewed_catalog_0031_clone",
    )
    connection = Mock()
    connection.in_transaction.return_value = in_transaction
    connection.scalar.return_value = owner
    monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
    monkeypatch.setattr(
        migration, "is_live_accounts_http_clone", lambda unused: marked_clone,
    )
    with pytest.raises(RuntimeError, match="CLONE ONLY"):
        getattr(migration, action)()
    connection.execute.assert_not_called()


def test_default_alembic_heads_exclude_clone_revision() -> None:
    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / "alembic.ini"))
    assert set(ScriptDirectory.from_config(config).get_heads()) == {
        "0012_session_bound_rls", "0049_yandex_id",
    }
