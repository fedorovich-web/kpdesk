"""Maintained safe profiles never broaden a rejected or incomplete selection."""
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import run_safe_tests as runner

ROOT = Path(__file__).resolve().parents[2]


def layout(tmp_path: Path) -> Path:
    backend = tmp_path / "backend"
    tests = backend / "tests"
    tests.mkdir(parents=True)
    (tests / "test_safe.py").write_text("# fake offline test\n", encoding="utf-8")
    (tests / "test_unsafe.py").write_text(
        (ROOT / "backend/tests/test_a2_legacy_quarantine.py").read_text(encoding="utf-8"), encoding="utf-8",
    )
    return backend


@pytest.mark.parametrize("requested", [["missing.py"], ["test_unsafe.py"], ["test_safe.py", "test_unsafe.py"]])
def test_rejected_selection_never_silently_becomes_a_subset(tmp_path: Path, requested: list[str]) -> None:
    with pytest.raises(runner.SelectionError):
        runner.select_tests(layout(tmp_path), "offline", requested)


def test_empty_directory_is_not_pytest_auto_discovery(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    with pytest.raises(runner.SelectionError):
        runner.select_tests(tmp_path, "offline", [])


def test_offline_selects_only_safe_files_and_reports_exclusions(tmp_path: Path) -> None:
    backend = layout(tmp_path)
    selected, excluded = runner.select_tests(backend, "offline", [])
    assert [p.name for p in selected] == ["test_safe.py"]
    assert excluded == ["test_unsafe.py"]


def test_sql_profile_contains_exact_reviewed_files(tmp_path: Path) -> None:
    backend = layout(tmp_path)
    for name in runner.REVIEWED_SQL_FILES:
        (backend / "tests" / name).write_text("# fake reviewed SQL file\n", encoding="utf-8")
    selected, _ = runner.select_tests(backend, "postgres-legacy", [])
    assert {p.name for p in selected} == set(runner.REVIEWED_SQL_FILES)
    with pytest.raises(runner.SelectionError):
        runner.select_tests(backend, "postgres-legacy", ["test_safe.py"])


def test_missing_reviewed_sql_file_blocks_incomplete_profile(tmp_path: Path) -> None:
    backend = layout(tmp_path)
    with pytest.raises(runner.SelectionError):
        runner.select_tests(backend, "postgres-legacy", [])


def test_unknown_profile_is_not_a_fallback(tmp_path: Path) -> None:
    with pytest.raises(runner.SelectionError):
        runner.select_tests(layout(tmp_path), "typo", [])


def test_default_offline_overrides_inherited_sql_flag_and_restores_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "BACKEND", layout(tmp_path))
    monkeypatch.setenv(runner.SQL_OPT_IN, "1")

    def invoke(args: list[str]) -> int:
        assert os.environ[runner.SQL_OPT_IN] == ""
        assert args[-1].endswith("test_safe.py")
        return 0

    mocked = Mock(side_effect=invoke)
    monkeypatch.setattr(pytest, "main", mocked)
    assert runner.main([]) == 0
    assert os.environ[runner.SQL_OPT_IN] == "1"
    mocked.assert_called_once()


def test_sql_profile_flag_is_removed_after_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = layout(tmp_path)
    for name in runner.REVIEWED_SQL_FILES:
        (backend / "tests" / name).write_text("# fake reviewed file", encoding="utf-8")
    monkeypatch.setattr(runner, "BACKEND", backend)
    monkeypatch.delenv(runner.SQL_OPT_IN, raising=False)

    def fail(args: list[str]) -> int:
        assert os.environ[runner.SQL_OPT_IN] == "1"
        raise RuntimeError("pytest failed")

    monkeypatch.setattr(pytest, "main", fail)
    with pytest.raises(RuntimeError, match="pytest failed"):
        runner.main(["--profile", "postgres-legacy"])
    assert runner.SQL_OPT_IN not in os.environ


def test_bad_cli_selection_never_invokes_pytest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "BACKEND", layout(tmp_path))
    invoked = Mock()
    monkeypatch.setattr(pytest, "main", invoked)
    assert runner.main(["missing.py"]) == 2
    invoked.assert_not_called()
