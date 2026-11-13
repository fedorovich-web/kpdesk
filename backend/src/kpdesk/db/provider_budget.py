"""PostgreSQL-backed global provider attempt ledger; no in-process fallback."""
from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, text

from kpdesk.db.authenticated_tenant import AuthenticatedTenantScope, bind_authenticated_tenant


@dataclass(frozen=True)
class ProviderLimits:
    requests_per_minute: int = 20
    requests_per_day: int = 50
    max_parallel: int = 1
    max_calls_per_operation: int = 6

    def __post_init__(self) -> None:
        for value, ceiling in ((self.requests_per_minute, 20),
                               (self.requests_per_day, 1000),
                               (self.max_parallel, 20),
                               (self.max_calls_per_operation, 6)):
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError("Invalid provider budget limit")


@dataclass(frozen=True)
class ProviderAttempt:
    id: UUID
    ordinal: int


class ProviderBudgetError(Exception):
    def __init__(self, reason: str, retry_after_seconds: int | None = None) -> None:
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds
        super().__init__(reason)


class SharedProviderBudget:
    def __init__(self, engine: Engine, *, organization_id: UUID,
                 session_cookie: str, csrf_token: str, operation_id: UUID,
                 fingerprint: str, limits: ProviderLimits):
        if not isinstance(operation_id, UUID) or not isinstance(organization_id, UUID):
            raise TypeError("Invalid budget identity")
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError("Fingerprint must be a SHA-256 hex digest")
        self.engine, self.organization_id = engine, organization_id
        self.session_cookie, self.csrf_token = session_cookie, csrf_token
        self.operation_id, self.fingerprint, self.limits = operation_id, fingerprint, limits
        self._claim = uuid4()

    def _bind(self, connection: Connection) -> AuthenticatedTenantScope:
        return bind_authenticated_tenant(connection, session_token=self.session_cookie,
            organization_id=self.organization_id, operation="write", csrf=self.csrf_token)

    @contextmanager
    def _transaction(self) -> Iterator[Connection]:
        with self.engine.begin() as connection:
            # Cancellation must wait for this worker transaction to settle;
            # neither authentication nor a competing ledger lock may wait forever.
            connection.exec_driver_sql("SET LOCAL statement_timeout='10s'")
            connection.exec_driver_sql("SET LOCAL lock_timeout='5s'")
            yield connection

    def reserve(self, input_chars: int) -> ProviderAttempt:
        attempt_id = uuid4()
        with self._transaction() as connection:
            self._bind(connection)
            row = connection.execute(text("""
                SELECT reason,ordinal,retry_after FROM public.kpdesk_provider_reserve(
                    :org,:op,:fingerprint,:claim,:attempt,:chars,:rpm,:day,:parallel,:calls)
            """), {"org": self.organization_id, "op": self.operation_id,
                "fingerprint": self.fingerprint, "claim": self._claim, "attempt": attempt_id,
                "chars": input_chars, "rpm": self.limits.requests_per_minute,
                "day": self.limits.requests_per_day, "parallel": self.limits.max_parallel,
                "calls": self.limits.max_calls_per_operation}).one()
        if row.reason != "reserved":
            raise ProviderBudgetError(row.reason, row.retry_after)
        return ProviderAttempt(attempt_id, row.ordinal)

    def finish(self, attempt: ProviderAttempt, *, outcome: str,
               http_status: int | None = None, elapsed_ms: int = 0,
               prompt_tokens: int | None = None, completion_tokens: int | None = None,
               total_tokens: int | None = None) -> None:
        with self._transaction() as connection:
            self._bind(connection)
            result = connection.scalar(text("""
                SELECT public.kpdesk_provider_finish(:org,:op,:claim,:attempt,:outcome,
                    :http,:elapsed,:prompt,:completion,:total)
            """), {"org": self.organization_id, "op": self.operation_id,
                "claim": self._claim, "attempt": attempt.id, "outcome": outcome,
                "http": http_status, "elapsed": elapsed_ms, "prompt": prompt_tokens,
                "completion": completion_tokens, "total": total_tokens})
        if result != "finished":
            raise ProviderBudgetError(str(result))

    def close(self, *, succeeded: bool, ambiguous: bool = False) -> None:
        with self._transaction() as connection:
            self._bind(connection)
            result = connection.scalar(text("""
                SELECT public.kpdesk_provider_close(:org,:op,:claim,:success,:ambiguous)
            """), {"org": self.organization_id, "op": self.operation_id,
                "claim": self._claim, "success": succeeded, "ambiguous": ambiguous})
        if result != "closed":
            raise ProviderBudgetError(str(result))
