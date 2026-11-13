"""Authenticated independent owner dispute arbitration; never gold authorization."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Cookie, Depends, File, Form, Header, Query, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from kpdesk.api.errors import ApiError
from kpdesk.api.tenant_candidate_review_routes import _verified_archive
from kpdesk.api.tenant_session_routes import SESSION_COOKIE, get_unreleased_auth_repository
from kpdesk.api.tenant_supplier_precision_routes import _store
from kpdesk.db.auth_sessions import AuthSessionRepository
from kpdesk.db.tenant_access import TenantAccessDenied
from kpdesk.matching.tenant_candidate_adjudication import TenantCandidateAdjudicationStore

router = APIRouter(prefix="/api/session/organizations/{organization_id}/candidate-adjudications")
_SHA = r"^[0-9a-f]{64}$"


class ArbitrationBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    archive_sha256: str = Field(pattern=_SHA)
    queue_sha256: str = Field(pattern=_SHA)
    task_sha256: str = Field(pattern=_SHA)
    request_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    finding: Literal["resolve_proposals", "needs_evidence", "reject_case"]
    accepted_skus: list[str] = Field(default_factory=list, max_length=20)
    rejected_skus: list[str] = Field(default_factory=list, max_length=20)
    no_candidate_match: bool = False
    rationale: str = Field(min_length=32, max_length=1000)
    personally_adjudicated: Literal[True]


class ArbitrationReceipt(BaseModel):
    recorded: bool
    queue_sha256: str
    task_sha256: str
    request_id: str
    finding: Literal["resolve_proposals", "needs_evidence", "reject_case"]
    adjudicator_user_id: uuid.UUID
    reviewer_independence_verified: bool = True
    rights_verified: bool = False
    privacy_review_complete: bool = False
    independent_gold: bool = False
    training_approved: bool = False
    benchmark_approved: bool = False


class ArbitrationHistoryRow(BaseModel):
    task_sha256: str
    source_sha256: str
    request_id: str
    candidate_skus: list[str]
    finding: Literal["resolve_proposals", "needs_evidence", "reject_case"]
    accepted_skus: list[str]
    rejected_skus: list[str]
    no_candidate_match: bool
    rationale: str
    adjudicator_user_id: uuid.UUID
    original_reviewer_count: int
    recorded_at: datetime


class ArbitrationHistory(BaseModel):
    rows: list[ArbitrationHistoryRow]
    next_offset: int | None
    independent_gold: bool = False
    training_approved: bool = False


@router.post("", status_code=201, response_model=ArbitrationReceipt)
def adjudicate(
    organization_id: uuid.UUID,
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    archive: Annotated[UploadFile, File(alias="evidence")],
    review_json: Annotated[str, Form()],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> ArbitrationReceipt:
    scope = _store(repository, session_cookie, organization_id, csrf=csrf, write=True)
    store = TenantCandidateAdjudicationStore(scope)
    try:
        store._authorize(write=True)
    except TenantAccessDenied as error:
        raise ApiError(403, "organization_forbidden", "У вас нет доступа к разрешению спорных оценок в этой организации.") from error
    except (RuntimeError, SQLAlchemyError) as error:
        raise ApiError(503, "adjudication_store_unavailable", "Не удалось получить или сохранить решение по спорной оценке. Проверьте историю решений перед повторной попыткой.") from error
    if len(review_json) > 8192:
        raise ApiError(422, "invalid_adjudication", "Решение по спорной оценке превышает допустимый объём. Проверьте выбранные товары и сократите обоснование.")
    try:
        body = ArbitrationBody.model_validate_json(review_json)
    except (ValueError, ValidationError) as error:
        raise ApiError(422, "invalid_adjudication", "Решение по спорной оценке не принято. Проверьте заявку, выбранные товары и обоснование.") from error
    archive_sha, original = _verified_archive(archive)
    if archive_sha != body.archive_sha256 or original.queue_sha256 != body.queue_sha256:
        raise ApiError(409, "source_changed", "Исходный архив или набор заявок изменился. Заново загрузите данные для разрешения спорной оценки.")
    task = original.tasks.get(body.request_id)
    from kpdesk.matching.request_annotations import _sha
    if task is None or _sha(task) != body.task_sha256:
        raise ApiError(422, "invalid_adjudication", "Эта заявка отсутствует в проверенном наборе или её данные изменились. Загрузите актуальные данные заново.")
    try:
        store.attest(
            original, request_id=body.request_id, task_sha256=body.task_sha256,
            finding=body.finding, accepted_skus=body.accepted_skus,
            rejected_skus=body.rejected_skus,
            no_candidate_match=body.no_candidate_match, rationale=body.rationale,
        )
    except (TypeError, ValueError) as error:
        raise ApiError(422, "invalid_adjudication", "Данные решения по спорной оценке не прошли проверку. Проверьте оценки товаров и обоснование.") from error
    except TenantAccessDenied as error:
        raise ApiError(409, "adjudication_conflict", "Не удалось подтвердить независимость решения руководителя. Проверьте доступ, исходные оценки и участие руководителя в предыдущих решениях.") from error
    except (RuntimeError, SQLAlchemyError) as error:
        raise ApiError(503, "adjudication_store_unavailable", "Не удалось получить или сохранить решение по спорной оценке. Проверьте историю решений перед повторной попыткой.") from error
    response.headers["Cache-Control"] = "no-store"
    return ArbitrationReceipt(
        recorded=True, queue_sha256=original.queue_sha256,
        task_sha256=body.task_sha256, request_id=body.request_id,
        finding=body.finding, adjudicator_user_id=scope.identity.user_id,
    )


@router.get("", response_model=ArbitrationHistory)
def adjudication_history(
    organization_id: uuid.UUID,
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    queue_sha256: Annotated[str, Query(pattern=_SHA)],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    offset: Annotated[int, Query(ge=0, le=5000)] = 0,
    limit: Annotated[int, Query(ge=1, le=50)] = 50,
) -> ArbitrationHistory:
    scope = _store(repository, session_cookie, organization_id, write=False)
    try:
        rows = TenantCandidateAdjudicationStore(scope).history(
            queue_sha256, limit=limit + 1, offset=offset,
        )
    except ValueError as error:
        raise ApiError(422, "invalid_adjudication_selector", "Не удалось определить набор спорных заявок. Выберите исходные данные заново.") from error
    except TenantAccessDenied as error:
        raise ApiError(403, "organization_forbidden", "У вас нет доступа к истории решений по спорным оценкам.") from error
    except (RuntimeError, SQLAlchemyError) as error:
        raise ApiError(503, "adjudication_store_unavailable", "Не удалось получить или сохранить решение по спорной оценке. Проверьте историю решений перед повторной попыткой.") from error
    response.headers["Cache-Control"] = "no-store"
    return ArbitrationHistory(
        rows=[ArbitrationHistoryRow.model_validate(row) for row in rows[:limit]],
        next_offset=offset + limit if len(rows) > limit else None,
    )
