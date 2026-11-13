"""Fail-closed admission and inactive rollout contract for revision 0033."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from kpdesk.db.review_schema_manifest import REVIEW_FUNCTIONS, REVIEW_TABLES

migration = importlib.import_module("migrations.versions.0033_reviewed_catalog_provenance")


@pytest.mark.parametrize("action", ["upgrade", "downgrade"])
def test_explicit_approval_required_before_database_access(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    connection = Mock()
    monkeypatch.setattr(
        migration.op,
        "get_context",
        lambda: SimpleNamespace(config=SimpleNamespace(attributes={})),
    )
    monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
    with pytest.raises(RuntimeError, match="explicit Alembic approval"):
        getattr(migration, action)()
    connection.execute.assert_not_called()
    connection.scalar.assert_not_called()


@pytest.mark.parametrize(
    "database,role,revision",
    [
        ("kpdesk_other", "kpdesk", "0032_supplier_quote_audit"),
        ("kpdesk", "kpdesk_tenant_runtime", "0032_supplier_quote_audit"),
        ("kpdesk", "kpdesk", "0031_catalog_taxonomy_v4"),
    ],
)
def test_unrecognized_target_refused_before_ddl(
    monkeypatch: pytest.MonkeyPatch,
    database: str,
    role: str,
    revision: str,
) -> None:
    connection = Mock()
    connection.in_transaction.return_value = True
    connection.execute.return_value.one.return_value = (database, role, revision)
    monkeypatch.setattr(
        migration.op,
        "get_context",
        lambda: SimpleNamespace(
            config=SimpleNamespace(
                attributes={"kpdesk_review_schema_0033_approved": True},
            )
        ),
    )
    monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
    monkeypatch.setattr(migration, "is_live_accounts_http_clone", lambda _: False)
    with pytest.raises(RuntimeError, match="target is unsafe"):
        migration.upgrade()
    connection.scalar.assert_not_called()
    assert connection.execute.call_count == 1


def test_new_revision_keeps_single_live_release_head() -> None:
    backend = Path(__file__).resolve().parents[1]
    script = ScriptDirectory.from_config(Config(str(backend / "alembic.ini")))
    assert set(script.get_heads()) == {
        "0012_session_bound_rls",
        "0047_auth_reset_serialization",
    }
    assert migration.down_revision == "0032_supplier_quote_audit"


def test_default_schema_rollout_is_inactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = Mock()
    connection.scalar.return_value = "public"
    monkeypatch.setattr(migration, "_preflight", lambda _: connection)
    monkeypatch.setattr(migration, "_check_inactive", lambda _: None)
    installers: list[str] = []
    for name in (
        "_install_checkpoint_objects",
        "_install_current_review_objects",
        "_install_paged_checkpoint_objects",
        "_install_checkpoint_probe_objects",
    ):
        monkeypatch.setattr(migration, name, lambda _conn, n=name: installers.append(n))

    def publication(_conn: object, *, signing_secret: bytes | None) -> None:
        assert signing_secret is None
        installers.append("publication")

    monkeypatch.setattr(migration, "_install_publication_objects", publication)
    migration.upgrade()
    assert len(installers) == 5
    assert len(REVIEW_TABLES) == 6
    statements = [str(call.args[0]) for call in connection.execute.call_args_list]
    assert sum("REVOKE ALL ON FUNCTION" in value for value in statements) == len(REVIEW_FUNCTIONS)
    assert all(
        "kpdesk_tenant_runtime" in value
        for value in statements
        if "REVOKE ALL ON FUNCTION" in value
    )
