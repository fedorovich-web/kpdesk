"""Transaction semantics: a denial commits counters, SQL errors still roll back.

Unit-level orchestration tests complement the real PostgreSQL/savepoint HTTP
tests; they do NOT simulate independent physical database connections.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from sqlalchemy import Connection, Engine

from kpdesk.db.auth_sessions import AuthSessionRepository, IssuedSession, LoginDenied
from kpdesk.db.committed_login import CommittedLogin, CommittedSessionRevocation


class _RecordingEngine:
    def __init__(self) -> None:
        self.events: list[str] = []

    @contextmanager
    def begin(self) -> Iterator[Connection]:
        self.events.append("begin")
        try:
            yield cast(Connection, object())
        except BaseException:
            self.events.append("rollback")
            raise
        else:
            self.events.append("commit")


def test_denied_login_does_not_undo_database_attempt_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _RecordingEngine()

    def reject(
        _: AuthSessionRepository, email: str, password: str, *, peer: str,
    ) -> IssuedSession:
        assert email == "unknown@invalid.test"
        assert peer == "192.0.2.3"
        engine.events.append("attempt_written")
        raise LoginDenied("Invalid login.")

    monkeypatch.setattr(AuthSessionRepository, "login", reject)
    with pytest.raises(LoginDenied):
        CommittedLogin(cast(Engine, engine)).attempt(
            "unknown@invalid.test", "not-a-real-secret", peer="192.0.2.3",
        )
    assert engine.events == ["begin", "attempt_written", "commit"]


def test_successful_login_returns_only_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _RecordingEngine()
    expected = IssuedSession(
        "a" * 43, "b" * 43, uuid.uuid4(),
        datetime.now(UTC) + timedelta(hours=12),
    )

    def accept(
        _: AuthSessionRepository, email: str, password: str, *, peer: str,
    ) -> IssuedSession:
        engine.events.append("session_inserted")
        return expected

    monkeypatch.setattr(AuthSessionRepository, "login", accept)
    issued = CommittedLogin(cast(Engine, engine)).attempt(
        "verified@invalid.test", "not-a-real-secret", peer="192.0.2.4",
    )
    assert issued is expected
    assert engine.events == ["begin", "session_inserted", "commit"]


def test_sql_error_rolls_back_instead_of_being_disguised_as_bad_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _RecordingEngine()

    def sql_failure(
        _: AuthSessionRepository, email: str, password: str, *, peer: str,
    ) -> IssuedSession:
        engine.events.append("sql_failed")
        raise RuntimeError("synthetic database error")

    monkeypatch.setattr(AuthSessionRepository, "login", sql_failure)
    with pytest.raises(RuntimeError, match="synthetic database error"):
        CommittedLogin(cast(Engine, engine)).attempt(
            "verified@invalid.test", "not-a-real-secret", peer="192.0.2.5",
        )
    assert engine.events == ["begin", "sql_failed", "rollback"]


def test_logout_revocation_is_committed_before_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _RecordingEngine()

    def revoke(_: AuthSessionRepository, token: str) -> bool:
        assert token == "a" * 43
        engine.events.append("session_revoked")
        return True

    monkeypatch.setattr(AuthSessionRepository, "revoke", revoke)
    assert CommittedSessionRevocation(cast(Engine, engine)).revoke("a" * 43)
    assert engine.events == ["begin", "session_revoked", "commit"]
