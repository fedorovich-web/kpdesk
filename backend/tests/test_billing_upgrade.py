"""Offline safety checks for the explicit schema-only billing upgrade operator."""
import hashlib
from pathlib import Path
from unittest.mock import Mock

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from scripts import upgrade_billing as operator


def test_default_preflight_and_apply_guards_before_database(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Mock(return_value={"status": "passed"})
    monkeypatch.setattr(operator, "run", run)
    assert operator.main([]) == 0
    run.assert_called_once_with(apply=False)
    run.reset_mock()
    with pytest.raises(SystemExit):
        operator.main(["--apply-0048"])
    run.assert_not_called()


def test_direct_apply_requires_all_confirmations_before_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    read = Mock(side_effect=AssertionError("secrets were read"))
    monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises(ValueError, match="billing_upgrade_confirmation_required"):
        operator.run(apply=True)
    read.assert_not_called()


def test_backup_requires_exact_archive_hash_and_custom_format(tmp_path: Path) -> None:
    archive = tmp_path / "database.dump"
    archive.write_bytes(b"PGDMP" + b"synthetic-backup" * 20)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert operator.verify_backup(archive, digest) == digest
    for bad_hash in ("", "x" * 64, "a" * 64, digest.upper()):
        with pytest.raises(ValueError):
            operator.verify_backup(archive, bad_hash)
    archive.write_bytes(b"plain-text-is-not-a-custom-dump")
    with pytest.raises(ValueError, match="billing_upgrade_backup_invalid"):
        operator.verify_backup(archive, hashlib.sha256(archive.read_bytes()).hexdigest())


def test_backup_requires_absolute_regular_path(tmp_path: Path) -> None:
    for path in (Path("relative.dump"), tmp_path, tmp_path / "missing.dump"):
        with pytest.raises(ValueError, match="billing_upgrade_backup_invalid"):
            operator.verify_backup(path, "a" * 64)


def test_failed_upgrade_sanitizes_errors(monkeypatch: pytest.MonkeyPatch,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(operator, "run", Mock(side_effect=RuntimeError("private-key-value")))
    assert operator.main([]) == 1
    output = capsys.readouterr().out
    assert "private-key-value" not in output
    assert "billing_upgrade_failed" in output


def test_apply_only_forwards_explicit_confirmations(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run = Mock(return_value={"status": "passed"})
    monkeypatch.setattr(operator, "run", run)
    backup = tmp_path / "database.dump"
    assert operator.main(["--apply-0048", "--confirm-maintenance", "--confirm-backup-restored",
                          "--backup", str(backup), "--backup-sha256", "a" * 64]) == 0
    run.assert_called_once_with(apply=True, maintenance=True, backup_restored=True,
                               backup=backup, backup_sha256="a" * 64)


def test_migration_identity_is_pinned_after_inventory_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    inventory = Mock()
    monkeypatch.setattr(operator, "migration_plan", inventory)
    config = Mock()
    directory = Mock()
    directory.get_revision.return_value = Mock(down_revision="0046_provider_authority")
    monkeypatch.setattr(ScriptDirectory, "from_config", Mock(return_value=directory))
    with pytest.raises(ValueError, match="billing_upgrade_lineage_invalid"):
        operator.migration_config(config)
    inventory.assert_called_once()


def test_source_requires_exact_0047_before_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = Mock()
    monkeypatch.setattr(operator, "verify_target", Mock(return_value=operator.TARGET))
    migration = Mock()
    monkeypatch.setattr(command, "upgrade", migration)
    with pytest.raises(ValueError, match="billing_upgrade_requires_0047"):
        operator.apply_schema(connection, "a" * 24, Mock())
    migration.assert_not_called()
