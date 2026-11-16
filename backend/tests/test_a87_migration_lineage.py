"""A87 production/test-only Alembic branches must remain distinct and explicit."""
from __future__ import annotations

from unittest.mock import Mock

import pytest
from alembic.script import ScriptDirectory

from scripts import audit_migration_lineage as lineage
from scripts import run_release_quality_gate as gate


def test_known_branches_have_single_shared_0005_fork() -> None:
    audited = lineage.audit_migration_lineage()
    assert audited.target == "live_release@head"
    assert audited.release_head == "0048_lava_billing"
    assert audited.historical_test_head == "0012_session_bound_rls"
    assert len(audited.release_revisions) == 36
    assert len(audited.historical_test_revisions) == 7
    assert set(audited.release_revisions).isdisjoint(
        audited.historical_test_revisions
    )
    assert audited.release_revisions[-1] == "0013_live_expand"
    assert audited.historical_test_revisions[-1] == "0006_legacy_quarantine"


def test_new_unreviewed_head_blocks_release_gate_before_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ScriptDirectory, "get_heads",
        lambda self: ["0012_session_bound_rls", "0036_supplier_registry", "unsafe"],
    )
    with pytest.raises(RuntimeError, match="Unexpected Alembic heads"):
        lineage.audit_migration_lineage()
    with pytest.raises(RuntimeError, match="Unexpected Alembic heads"):
        gate._assert_safe_clone_source()


def test_missing_live_release_branch_blocks_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = ScriptDirectory.get_revision

    def altered(self: ScriptDirectory, revision: str):  # type: ignore[no-untyped-def]
        if revision == "live_release@head":
            return Mock(revision="0012_session_bound_rls")
        return original(self, revision)

    monkeypatch.setattr(ScriptDirectory, "get_revision", altered)
    with pytest.raises(RuntimeError, match="Active live_release"):
        lineage.audit_migration_lineage()


def test_cli_audit_is_read_only(capsys: pytest.CaptureFixture[str]) -> None:
    assert lineage.main() == 0
    output = capsys.readouterr().out
    assert "read-only" in output
    assert "live_release@head -> 0048_lava_billing" in output
    assert "0012_session_bound_rls" in output
