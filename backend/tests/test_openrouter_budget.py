from collections.abc import Iterator
from contextlib import contextmanager
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import Engine


def test_provider_limits_enforce_hard_ceilings() -> None:
    from kpdesk.db.provider_budget import ProviderLimits

    assert ProviderLimits().requests_per_day == 50
    for kwargs in ({"requests_per_minute": 21}, {"requests_per_day": 1001},
                   {"max_parallel": 0}, {"max_calls_per_operation": 7}):
        with pytest.raises(ValueError):
            ProviderLimits(**kwargs)


def test_attempt_and_error_expose_contract() -> None:
    from kpdesk.db.provider_budget import ProviderAttempt, ProviderBudgetError

    attempt_id = uuid4()
    assert ProviderAttempt(attempt_id, 1).id == attempt_id
    error = ProviderBudgetError("minute_quota", 12)
    assert (error.reason, error.retry_after_seconds) == ("minute_quota", 12)


class _Engine:
    def __init__(self, row: SimpleNamespace) -> None:
        self.row = row
        self.committed = False
        self.statements: list[str] = []

    @contextmanager
    def begin(self) -> Iterator[_Engine]:
        yield self
        self.committed = True

    def execute(self, statement: object, parameters: dict[str, Any]) -> _Engine:
        self.parameters = parameters
        return self

    def one(self) -> SimpleNamespace:
        return self.row

    def exec_driver_sql(self, statement: str) -> None:
        self.statements.append(statement)

    def scalar(self, statement: object, parameters: dict[str, Any]) -> str:
        return str(self.row.scalar_value)


@pytest.mark.parametrize('operation', ['reserve', 'finish', 'close'])
def test_budget_sql_waits_are_bounded_before_authentication(
    monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    from kpdesk.db import provider_budget as budget

    engine = _Engine(SimpleNamespace(reason='reserved', ordinal=1, retry_after=None,
                                    scalar_value='finished' if operation == 'finish' else 'closed'))

    def authenticated(*args: object, **kwargs: object) -> None:
        assert engine.statements == ["SET LOCAL statement_timeout='10s'", "SET LOCAL lock_timeout='5s'"]

    monkeypatch.setattr(budget, 'bind_authenticated_tenant', authenticated)
    provider = budget.SharedProviderBudget(cast(Engine, engine), organization_id=uuid4(),
        session_cookie='cookie', csrf_token='csrf', operation_id=uuid4(),
        fingerprint='a' * 64, limits=budget.ProviderLimits())
    if operation == 'reserve':
        provider.reserve(42)
    elif operation == 'finish':
        provider.finish(budget.ProviderAttempt(uuid4(), 1), outcome='cancelled')
    else:
        provider.close(succeeded=False)
    assert engine.committed


def test_denied_reservation_commits_before_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    from kpdesk.db import provider_budget as budget

    engine = _Engine(SimpleNamespace(reason="minute_quota", ordinal=None, retry_after=60))
    monkeypatch.setattr(budget, "bind_authenticated_tenant", lambda *a, **k: None)
    provider = budget.SharedProviderBudget(cast(Engine, engine), organization_id=uuid4(),
        session_cookie="cookie", csrf_token="csrf", operation_id=uuid4(),
        fingerprint="a" * 64, limits=budget.ProviderLimits())
    with pytest.raises(budget.ProviderBudgetError) as exc:
        provider.reserve(42)
    assert engine.committed
    assert exc.value.retry_after_seconds == 60


def test_reservation_returns_attempt_and_preserves_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    from kpdesk.db import provider_budget as budget

    engine = _Engine(SimpleNamespace(reason="reserved", ordinal=1, retry_after=None))
    monkeypatch.setattr(budget, "bind_authenticated_tenant", lambda *a, **k: None)
    provider = budget.SharedProviderBudget(cast(Engine, engine), organization_id=uuid4(),
        session_cookie="cookie", csrf_token="csrf", operation_id=uuid4(),
        fingerprint="a" * 64, limits=budget.ProviderLimits())
    first = provider.reserve(42)
    claim = engine.parameters["claim"]
    second = provider.reserve(42)
    assert first.id != second.id
    assert first.ordinal == 1
    assert engine.parameters["claim"] == claim


def test_cache_claim_preserves_reviewed_protocol_and_authorization() -> None:
    from kpdesk.db.provider_budget_schema import CACHE_CLAIM

    source = (Path(__file__).parents[1] / "migrations/versions/0030_tenant_llm_cache.py")
    old = source.read_text(encoding="utf-8").split(
        "      CREATE FUNCTION public.kpdesk_llm_cache_claim(", 1
    )[1].split('    """)', 1)[0]
    old = "CREATE OR REPLACE FUNCTION public.kpdesk_llm_cache_claim(" + old
    old = old.replace("p_model NOT IN (\n             'openai/gpt-oss-120b',"
                      "'openai/gpt-oss-20b','qwen/qwen3.8-27b'\n           )",
                      "p_model <> 'nvidia/nemotron-3-ultra-550b-a55b:free'")
    old = old.replace("interval '45 seconds'", "interval '300 seconds'")
    assert CACHE_CLAIM == old


def test_logical_reserve_keeps_historical_quota_logic() -> None:
    versions = Path(__file__).parents[1] / "migrations/versions"
    source = (versions / "0019_tenant_llm_budget.py").read_text(encoding="utf-8")
    original = "CREATE OR REPLACE FUNCTION public.kpdesk_llm_reserve(" + source.split(
        "        CREATE FUNCTION public.kpdesk_llm_reserve(", 1
    )[1].split('    """)', 1)[0]
    original = original.replace(
        "p_model NOT IN (\n                    'openai/gpt-oss-120b','openai/gpt-oss-20b',"
        "\n                    'qwen/qwen3.8-27b'\n                )",
        "p_model <> 'nvidia/nemotron-3-ultra-550b-a55b:free'",
    ).replace("interval '45 seconds'", "interval '300 seconds'")
    spec = spec_from_file_location("budget_migration_test", versions / "0042_openrouter_budget.py")
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module._RESERVE == original
