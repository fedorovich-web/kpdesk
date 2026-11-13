"""Default offline session plus explicitly reviewed disposable SQL profile."""
import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from llm_test_support import MockBudget

from kpdesk.llm.client import llm_operation
from scripts.pytest_safety import SessionSafety

_SAFETY = pytest.StashKey[SessionSafety]()


def pytest_configure(config: pytest.Config) -> None:
    guard = SessionSafety(allow_postgres=os.environ.get("KP_DESK_RUN_ISOLATED_LEGACY_PG") == "1")
    config.stash[_SAFETY] = guard
    try:
        guard.start()
    except Exception:
        guard.close()
        raise


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    guard = session.config.stash.get(_SAFETY, None)
    if guard is not None and guard.owned:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line("Test safety: retained owned clones after guarded cleanup refusal: "
                                + ", ".join(sorted(guard.owned)), red=True)


def pytest_unconfigure(config: pytest.Config) -> None:
    guard = config.stash.get(_SAFETY, None)
    if guard is not None:
        guard.close()


@pytest_asyncio.fixture
async def mock_llm_operation(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> AsyncIterator[None]:
    monkeypatch.setattr("kpdesk.api.provider_scope.SharedProviderBudget", MockBudget)
    if hasattr(request.module, "TestClient"):
        yield
    else:
        async with llm_operation(MockBudget()):
            yield
