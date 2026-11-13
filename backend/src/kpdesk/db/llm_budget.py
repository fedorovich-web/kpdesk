"""Database-backed tenant LLM request quotas, independent of app processes.

A successful reservation always consumes an attempt and its submitted input
characters even if the provider fails; no guarantee of billable spend is made.
The ledger records only lengths, model and authenticated user/org identifiers,
never customer text or provider secrets.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Connection, Engine, text

from kpdesk.db.auth_sessions import AuthSessionRepository, _token_digest
from kpdesk.db.authenticated_tenant import bind_authenticated_tenant
from kpdesk.db.tenant_access import TenantAccessDenied


class InferenceQuotaExceeded(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@contextmanager
def inference_transaction(engine: Engine) -> Iterator[Connection]:
    with engine.begin() as connection:
        connection.exec_driver_sql("SET LOCAL statement_timeout='10s'")
        connection.exec_driver_sql("SET LOCAL lock_timeout='5s'")
        yield connection


def reserve_inference(
    engine: Engine, *,
    session_cookie: str | None, csrf: str | None,
    organization_id: uuid.UUID, model: str, input_chars: int,
) -> uuid.UUID:
    request_id = uuid.uuid4()
    with inference_transaction(engine) as connection:
        repository = AuthSessionRepository(connection)
        verified = repository.authenticate(
            session_cookie or "", operation="write", csrf=csrf,
        )
        scoped = bind_authenticated_tenant(
            connection, session_token=session_cookie or "",
            organization_id=organization_id, operation="write", csrf=csrf,
        )
        if scoped.identity.user_id != verified.user_id:
            raise TenantAccessDenied("Inference session identity mismatch.")
        decision = connection.scalar(text("""
            SELECT public.kpdesk_llm_reserve(:org,:id,:model,:length)
        """), {
            "org": organization_id,
            "id": request_id,
            "model": model,
            "length": input_chars,
        })
        if decision != "reserved":
            raise InferenceQuotaExceeded(str(decision))
    return request_id


def finish_inference(
    engine: Engine, *,
    request_id: uuid.UUID, session_cookie: str, succeeded: bool,
) -> None:
    digest = _token_digest(session_cookie)
    with inference_transaction(engine) as connection:
        acknowledged = connection.scalar(text("""
            SELECT public.kpdesk_llm_finish(:id,:digest,:ok)
        """), {"id": request_id, "digest": digest, "ok": succeeded})
        if acknowledged is not True:
            raise RuntimeError("LLM reservation could not be completed.")

def seal_inference_preview(
    engine: Engine, *, request_id: uuid.UUID,
    session_cookie: str, source: str, extracted: object,
) -> None:
    """Bind an emitted preview to one user, organization and source digest."""
    import hashlib

    from kpdesk.quotes.storage import _digest

    digest = _token_digest(session_cookie)
    with inference_transaction(engine) as connection:
        accepted = connection.scalar(text("""
            SELECT public.kpdesk_llm_preview_seal(:id,:session,:source,:preview)
        """), {
            "id": request_id, "session": digest,
            "source": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            "preview": _digest(extracted),
        })
        if accepted is not True:
            raise RuntimeError("Unable to seal an inference preview.")
