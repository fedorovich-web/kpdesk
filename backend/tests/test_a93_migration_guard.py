"""A93 deployment guard contracts for an excluded Alembic candidate."""
from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory


@pytest.mark.parametrize("method", ["upgrade", "downgrade"])
def test_candidate_refuses_missing_approval(
    monkeypatch: pytest.MonkeyPatch, method: str,
) -> None:
    migration = importlib.import_module(
        "migrations.candidate_versions.review_schema_0031_candidate"
    )
    connection = Mock()
    monkeypatch.setattr(
        migration.op, "get_context",
        lambda: SimpleNamespace(config=SimpleNamespace(attributes={})),
    )
    monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
    with pytest.raises(RuntimeError, match="explicit approval"):
        getattr(migration, method)()
    connection.execute.assert_not_called()
    connection.scalar.assert_not_called()


@pytest.mark.parametrize("method", ["upgrade", "downgrade"])
def test_candidate_rejects_nonowner_after_approval(
    monkeypatch: pytest.MonkeyPatch, method: str,
) -> None:
    migration = importlib.import_module(
        "migrations.candidate_versions.review_schema_0031_candidate"
    )
    connection = Mock()
    connection.in_transaction.return_value = True
    connection.scalar.return_value = "kpdesk_tenant_runtime"
    monkeypatch.setattr(
        migration.op, "get_context",
        lambda: SimpleNamespace(config=SimpleNamespace(
            attributes={"a93_schema_candidate_approved": True},
        )),
    )
    monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
    with pytest.raises(RuntimeError, match="unsafe"):
        getattr(migration, method)()
    connection.execute.assert_not_called()


def test_unapproved_candidate_not_in_default_migration_graph() -> None:
    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / "alembic.ini"))
    assert set(ScriptDirectory.from_config(config).get_heads()) == {
        "0012_session_bound_rls", "0049_yandex_id",
    }
