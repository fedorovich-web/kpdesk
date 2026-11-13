"""Explicit test-only shared-budget double, never imported by application code."""
from typing import Any
from uuid import uuid4

from kpdesk.db.provider_budget import ProviderAttempt


class MockBudget:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.attempts = 0
        self.finished: list[dict[str, Any]] = []
        self.closed: list[dict[str, Any]] = []

    def reserve(self, input_chars: int) -> ProviderAttempt:
        self.attempts += 1
        return ProviderAttempt(uuid4(), self.attempts)

    def finish(self, attempt: ProviderAttempt, **kwargs: Any) -> None:
        self.finished.append(kwargs)

    def close(self, **kwargs: Any) -> None:
        self.closed.append(kwargs)
