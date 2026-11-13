"""Release-gate contract: no green result with skipped tests or implicit clones."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest

from scripts import run_release_quality_gate as gate


@pytest.mark.parametrize("skipped,expected", [
    (0, pytest.ExitCode.OK),
    (1, pytest.ExitCode.TESTS_FAILED),
    (5, pytest.ExitCode.TESTS_FAILED),
])
def test_release_gate_rejects_every_unexecuted_test(
    skipped: int, expected: pytest.ExitCode,
) -> None:
    reporter = SimpleNamespace(stats={"skipped": [object() for _ in range(skipped)]})
    plugins = Mock()
    plugins.get_plugin.return_value = reporter
    session = cast(pytest.Session, SimpleNamespace(
        config=SimpleNamespace(pluginmanager=plugins),
        exitstatus=pytest.ExitCode.OK,
    ))
    gate.RejectSkipped().pytest_sessionfinish(session, int(pytest.ExitCode.OK))
    assert session.exitstatus == expected
    plugins.get_plugin.assert_called_once_with("terminalreporter")


def test_release_gate_refuses_missing_explicit_clone_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["run_release_quality_gate"])
    assert gate.main() == 2


def test_release_gate_enables_all_isolated_integrations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["run_release_quality_gate", "--disposable-clones"])
    for name in gate._OPT_IN_FLAGS:
        monkeypatch.setenv(name, "test-unset")
    preflight = Mock()
    monkeypatch.setattr(gate, "_assert_safe_clone_source", preflight)
    monkeypatch.setattr(os, "chdir", Mock())
    execute = Mock(return_value=pytest.ExitCode.OK)
    monkeypatch.setattr(pytest, "main", execute)
    assert gate.main() == 0
    preflight.assert_called_once_with()
    for name in gate._OPT_IN_FLAGS:
        assert os.environ[name] == "1"
    args, kwargs = execute.call_args
    assert args[0] == ["-q", "-rs", "--tb=short"]
    assert len(kwargs["plugins"]) == 1
    assert isinstance(kwargs["plugins"][0], gate.RejectSkipped)
