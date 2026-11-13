"""HTTPS tenant-only candidate judgment capture (never ML-gold promotion)."""
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Cookie, Depends, File, Form, Header, Query, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from kpdesk.api.errors import ApiError
from kpdesk.api.tenant_session_routes import SESSION_COOKIE, get_unreleased_auth_repository
from kpdesk.api.tenant_supplier_precision_routes import _store
from kpdesk.db.auth_sessions import AuthSessionRepository
from kpdesk.db.tenant_access import TenantAccessDenied
from kpdesk.matching.request_annotations import CandidateDecision, _sha, validate_review_batch
from kpdesk.matching.tenant_candidate_consensus import calculate_tenant_consensus
from kpdesk.matching.tenant_candidate_evidence import (
    VerifiedCandidateEvidence,
    verify_candidate_archive,
)
from kpdesk.matching.tenant_candidate_storage import TenantCandidateReviewStore
from kpdesk.matching.tenant_source_restrictions import TenantSourceRestrictionStore

router = APIRouter(prefix="/api/session/organizations/{organization_id}/candidate-reviews")
_SHA = r"^[0-9a-f]{64}$"


class CandidateReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    archive_sha256: str = Field(pattern=_SHA)
    queue_sha256: str = Field(pattern=_SHA)
    task_sha256: str = Field(pattern=_SHA)
    request_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    accepted_skus: list[str] = Field(default_factory=list, max_length=20)
    rejected_skus: list[str] = Field(default_factory=list, max_length=20)
    unresolved_skus: list[str] = Field(default_factory=list, max_length=20)
    no_candidate_match: bool
    rationale: str = Field(min_length=32, max_length=1000)
    personally_reviewed: Literal[True]


class CandidateReviewReceipt(BaseModel):
    recorded: bool
    queue_sha256: str
    task_sha256: str
    request_id: str
    reviewer_user_id: uuid.UUID
    independently_verified: bool = False
    rights_verified: bool = False
    privacy_review_complete: bool = False
    independent_gold: bool = False
    training_approved: bool = False
    benchmark_approved: bool = False


class CandidateReviewHistoryRow(BaseModel):
    task_sha256: str
    request_id: str
    source_sha256: str
    source_batch_id: str
    partition: Literal["train", "validation"]
    candidate_skus: list[str]
    accepted_skus: list[str]
    rejected_skus: list[str]
    unresolved_skus: list[str]
    no_candidate_match: bool
    rationale: str
    reviewer_user_id: uuid.UUID
    recorded_at: datetime


class CandidateReviewHistory(BaseModel):
    rows: list[CandidateReviewHistoryRow]
    next_offset: int | None
    independent_gold: bool = False
    training_approved: bool = False


class CandidateInspectProduct(BaseModel):
    sku: str
    name: str
    sale_unit: Literal["штука", "пачка", "коробка"]
    pieces_per_sale_unit: int


class CandidateInspectRow(BaseModel):
    request_id: str
    task_sha256: str
    request_text: str
    partition: Literal["train", "validation"]
    source_sha256: str
    source_batch_id: str
    candidate_skus: list[str]
    candidates: list[CandidateInspectProduct]


class CandidateInspectResponse(BaseModel):
    archive_sha256: str
    queue_sha256: str
    total_requests: int
    offset: int
    next_offset: int | None
    tasks: list[CandidateInspectRow]
    rights_verified: bool = False
    privacy_review_complete: bool = False
    independent_gold: bool = False
    training_approved: bool = False


def _verified_archive(archive: UploadFile) -> tuple[str, VerifiedCandidateEvidence]:
    if not (archive.filename or "").lower().endswith(".zip"):
        raise ApiError(422, "invalid_candidate_archive", "Выберите ZIP-архив с исходными заявками и данными для проверки подбора.")
    payload = archive.file.read(8_000_001)
    if len(payload) > 8_000_000 or not payload:
        raise ApiError(413, "candidate_archive_too_large", "Архив пуст или превышает допустимый размер. Выберите непустой архив в пределах лимита загрузки.")
    try:
        evidence = verify_candidate_archive(payload)
    except (ValueError, TypeError, OSError) as error:
        raise ApiError(422, "candidate_evidence_rejected", "Данные для проверки подбора не прошли проверку. Сверьте исходный архив, заявки и список товаров.") from error
    return hashlib.sha256(payload).hexdigest(), evidence


@router.post("/inspect", response_model=CandidateInspectResponse)
def inspect_candidate_review(
    organization_id: uuid.UUID,
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    archive: Annotated[UploadFile, File(alias="evidence")],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    offset: Annotated[int, Form(ge=0, le=200)] = 0,
    limit: Annotated[int, Form(ge=1, le=50)] = 25,
) -> CandidateInspectResponse:
    scope = _store(repository, session_cookie, organization_id, csrf=csrf, write=True)
    try:
        TenantCandidateReviewStore(scope)._authorize(write=True)
    except TenantAccessDenied as error:
        raise ApiError(403, "organization_forbidden", "У вас нет доступа к проверке подбора в этой организации.") from error
    except (RuntimeError, SQLAlchemyError) as error:
        raise ApiError(503, "candidate_store_unavailable", "Не удалось получить или сохранить оценки подбора. Проверьте историю оценок перед повторной попыткой.") from error
    archive_sha256, evidence = _verified_archive(archive)
    tasks = [
        CandidateInspectRow(
            request_id=request_id,
            task_sha256=_sha(task),
            request_text=task["request_text"],
            partition=task["partition"],
            source_sha256=task["source_sha256"],
            source_batch_id=task["source_batch_id"],
            candidate_skus=task["candidate_skus"],
            candidates=[
                CandidateInspectProduct(
                    sku=sku, name=evidence.catalog[sku].name,
                    sale_unit=evidence.catalog[sku].sale_unit,
                    pieces_per_sale_unit=evidence.catalog[sku].pieces_per_sale_unit,
                ) for sku in task["candidate_skus"]
            ],
        ) for request_id, task in sorted(evidence.tasks.items())
    ]
    response.headers["Cache-Control"] = "no-store"
    return CandidateInspectResponse(
        archive_sha256=archive_sha256, queue_sha256=evidence.queue_sha256,
        total_requests=len(tasks), offset=offset,
        next_offset=offset + limit if offset + limit < len(tasks) else None,
        tasks=tasks[offset:offset + limit],
    )


@router.post("", status_code=201, response_model=CandidateReviewReceipt)
def record_candidate_review(
    organization_id: uuid.UUID,
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    archive: Annotated[UploadFile, File(alias="evidence")],
    review_json: Annotated[str, Form()],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> CandidateReviewReceipt:
    scope = _store(repository, session_cookie, organization_id, csrf=csrf, write=True)
    store = TenantCandidateReviewStore(scope)
    try:
        store._authorize(write=True)
    except TenantAccessDenied as error:
        raise ApiError(403, "organization_forbidden", "У вас нет доступа к проверке подбора в этой организации.") from error
    except (RuntimeError, SQLAlchemyError) as error:
        raise ApiError(503, "candidate_store_unavailable", "Не удалось получить или сохранить оценки подбора. Проверьте историю оценок перед повторной попыткой.") from error
    if len(review_json) > 8192:
        raise ApiError(422, "invalid_candidate_review", "Данные решения превышают допустимый объём. Проверьте поля и сократите обоснование.")
    try:
        body = CandidateReviewBody.model_validate_json(review_json)
    except (ValueError, ValidationError) as error:
        raise ApiError(422, "invalid_candidate_review", "Решение менеджера не принято. Проверьте выбранную заявку, оценки товаров и обоснование.") from error
    archive_sha256, evidence = _verified_archive(archive)
    if archive_sha256 != body.archive_sha256:
        raise ApiError(409, "source_changed", "Архив изменился после проверки. Выберите исходный архив или проверьте новый заново.")
    try:
        if evidence.queue_sha256 != body.queue_sha256:
            raise ApiError(409, "queue_changed", "Список заявок и товаров для проверки изменился. Загрузите актуальные данные и проверьте оценки заново.")
        decision = CandidateDecision(
            request_id=body.request_id,
            task_sha256=body.task_sha256,
            reviewed_source="human_review",
            reviewed_by=str(scope.identity.user_id),
            reviewed_at=datetime.now(UTC),
            rationale=body.rationale,
            accepted_skus=tuple(body.accepted_skus),
            rejected_skus=tuple(body.rejected_skus),
            unresolved_skus=tuple(body.unresolved_skus),
            no_candidate_match=body.no_candidate_match,
        )
        validate_review_batch(evidence.queue, [decision.model_dump(mode="json")])
    except ApiError:
        raise
    except (ValueError, TypeError, ValidationError, OSError) as error:
        raise ApiError(422, "candidate_evidence_rejected", "Данные для проверки подбора не прошли проверку. Сверьте исходный архив, заявки и список товаров.") from error
    try:
        store.attest(evidence, decision)
    except (ValueError, TypeError) as error:
        raise ApiError(422, "invalid_candidate_review", "Оценки товаров не приняты. Проверьте заявку, выбранные товары и обоснование решения.") from error
    except TenantAccessDenied as error:
        raise ApiError(409, "candidate_review_conflict", "Не удалось подтвердить сохранение оценки. Проверьте доступ, актуальность заявки и историю решений перед повторной попыткой.") from error
    except (RuntimeError, SQLAlchemyError) as error:
        raise ApiError(503, "candidate_store_unavailable", "Не удалось получить или сохранить оценки подбора. Проверьте историю оценок перед повторной попыткой.") from error
    response.headers["Cache-Control"] = "no-store"
    return CandidateReviewReceipt(
        recorded=True, queue_sha256=evidence.queue_sha256,
        task_sha256=decision.task_sha256, request_id=decision.request_id,
        reviewer_user_id=scope.identity.user_id,
    )


@router.get("", response_model=CandidateReviewHistory)
def list_candidate_reviews(
    organization_id: uuid.UUID,
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    queue_sha256: Annotated[str, Query(pattern=_SHA)],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    offset: Annotated[int, Query(ge=0, le=5000)] = 0,
    limit: Annotated[int, Query(ge=1, le=50)] = 50,
) -> CandidateReviewHistory:
    scope = _store(repository, session_cookie, organization_id, write=False)
    try:
        records = TenantCandidateReviewStore(scope).history(
            queue_sha256, offset=offset, limit=limit + 1,
        )
    except ValueError as error:
        raise ApiError(422, "invalid_candidate_queue", "Не удалось определить набор заявок для проверки. Выберите исходный архив заново.") from error
    except TenantAccessDenied as error:
        raise ApiError(403, "organization_forbidden", "У вас нет доступа к проверке подбора в этой организации.") from error
    except (RuntimeError, SQLAlchemyError) as error:
        raise ApiError(503, "candidate_store_unavailable", "Не удалось получить или сохранить оценки подбора. Проверьте историю оценок перед повторной попыткой.") from error
    response.headers["Cache-Control"] = "no-store"
    return CandidateReviewHistory(
        rows=[CandidateReviewHistoryRow.model_validate(row) for row in records[:limit]],
        next_offset=offset + limit if len(records) > limit else None,
    )



class SourceRestrictionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    archive_sha256: str = Field(pattern=_SHA)
    queue_sha256: str = Field(pattern=_SHA)
    source_sha256: str = Field(pattern=_SHA)
    review_kind: Literal["rights", "privacy"]
    finding: Literal["requires_evidence", "restricted", "rejected"]
    evidence_sha256: str | None = Field(default=None, pattern=_SHA)
    rationale: str = Field(min_length=32, max_length=1000)
    personally_reviewed: Literal[True]


class SourceRestrictionReceipt(BaseModel):
    recorded: bool
    source_sha256: str
    review_kind: Literal["rights", "privacy"]
    finding: Literal["requires_evidence", "restricted", "rejected"]
    reviewer_user_id: uuid.UUID
    rights_verified: bool = False
    privacy_review_complete: bool = False
    training_approved: bool = False


class SourceRestrictionRow(BaseModel):
    source_sha256: str
    review_kind: Literal["rights", "privacy"]
    finding: Literal["requires_evidence", "restricted", "rejected"]
    evidence_sha256: str | None
    rationale: str
    reviewer_user_id: uuid.UUID
    recorded_at: datetime


class SourceRestrictionHistory(BaseModel):
    rows: list[SourceRestrictionRow]
    next_offset: int | None
    rights_verified: bool = False
    privacy_review_complete: bool = False
    training_approved: bool = False


class CandidateConsensusRow(BaseModel):
    request_id: str
    task_sha256: str
    reviewer_count: int
    reviewer_user_ids: list[str]
    accepted_skus: list[str] | None = None
    rejected_skus: list[str] | None = None
    no_candidate_match_among_proposals: bool | None = None


class CandidateConsensusResponse(BaseModel):
    queue_sha256: str
    source_bound_review_count: int
    agreed: list[CandidateConsensusRow]
    conflicts: list[CandidateConsensusRow]
    incomplete: list[CandidateConsensusRow]
    distinct_authenticated_reviewers_required: int = 2
    origin_independently_verified: bool = False
    rights_verified: bool = False
    privacy_review_complete: bool = False
    all_catalog_relevance_verified: bool = False
    independent_gold: bool = False
    training_approved: bool = False
    benchmark_approved: bool = False


@router.get("/consensus", response_model=CandidateConsensusResponse)
def view_candidate_consensus(
    organization_id: uuid.UUID,
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    queue_sha256: Annotated[str, Query(pattern=_SHA)],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> CandidateConsensusResponse:
    scope = _store(repository, session_cookie, organization_id, write=False)
    try:
        report = calculate_tenant_consensus(
            TenantCandidateReviewStore(scope), queue_sha256,
        )
    except ValueError as error:
        raise ApiError(422, "candidate_consensus_invalid", "Не удалось сопоставить оценки менеджеров. Проверьте выбранный набор заявок и сохранённые решения.") from error
    except TenantAccessDenied as error:
        raise ApiError(403, "organization_forbidden", "У вас нет доступа к проверке подбора в этой организации.") from error
    except (RuntimeError, SQLAlchemyError) as error:
        raise ApiError(503, "candidate_store_unavailable", "Не удалось получить или сохранить оценки подбора. Проверьте историю оценок перед повторной попыткой.") from error
    response.headers["Cache-Control"] = "no-store"
    return CandidateConsensusResponse.model_validate(report)


@router.post("/source-restrictions", status_code=201, response_model=SourceRestrictionReceipt)
def record_source_restriction(
    organization_id: uuid.UUID,
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    archive: Annotated[UploadFile, File(alias="evidence")],
    review_json: Annotated[str, Form()],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> SourceRestrictionReceipt:
    scope = _store(repository, session_cookie, organization_id, csrf=csrf, write=True)
    store = TenantSourceRestrictionStore(scope)
    try:
        store._authorize(write=True)
    except TenantAccessDenied as error:
        raise ApiError(403, "organization_forbidden", "У вас нет доступа к подтверждению сведений об исходных заявках.") from error
    except (RuntimeError, SQLAlchemyError) as error:
        raise ApiError(503, "restriction_store_unavailable", "Не удалось получить или сохранить проверку исходных документов. Проверьте историю решений перед повторной попыткой.") from error
    if len(review_json) > 8192:
        raise ApiError(422, "invalid_source_restriction", "Данные решения превышают допустимый объём. Проверьте поля и сократите обоснование.")
    try:
        body = SourceRestrictionBody.model_validate_json(review_json)
    except (ValueError, ValidationError) as error:
        raise ApiError(422, "invalid_source_restriction", "Данные проверки исходного документа не приняты. Проверьте выбранный документ, решение и обоснование.") from error
    archive_sha256, original = _verified_archive(archive)
    if archive_sha256 != body.archive_sha256 or original.queue_sha256 != body.queue_sha256:
        raise ApiError(409, "source_changed", "Данные исходного документа или набора заявок изменились. Загрузите актуальный архив и проверьте решение заново.")
    if body.source_sha256 not in original.source_sha256s:
        raise ApiError(422, "invalid_source_restriction", "Выбранный исходный документ отсутствует в проверенном архиве. Выберите правильный архив и повторите проверку.")
    try:
        store.attest(
            original, source_sha256=body.source_sha256,
            review_kind=body.review_kind, finding=body.finding,
            evidence_sha256=body.evidence_sha256, rationale=body.rationale,
        )
    except (ValueError, TypeError) as error:
        raise ApiError(422, "invalid_source_restriction", "Не удалось подтвердить решение по исходному документу. Проверьте данные и обоснование.") from error
    except TenantAccessDenied as error:
        raise ApiError(409, "restriction_conflict", "Не удалось подтвердить сохранение решения по исходному документу. Проверьте доступ и историю решений перед повторной попыткой.") from error
    except (RuntimeError, SQLAlchemyError) as error:
        raise ApiError(503, "restriction_store_unavailable", "Не удалось получить или сохранить проверку исходных документов. Проверьте историю решений перед повторной попыткой.") from error
    response.headers["Cache-Control"] = "no-store"
    return SourceRestrictionReceipt(
        recorded=True, source_sha256=body.source_sha256,
        review_kind=body.review_kind, finding=body.finding,
        reviewer_user_id=scope.identity.user_id,
    )


@router.get("/source-restrictions", response_model=SourceRestrictionHistory)
def view_source_restrictions(
    organization_id: uuid.UUID,
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    queue_sha256: Annotated[str, Query(pattern=_SHA)],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    offset: Annotated[int, Query(ge=0, le=5000)] = 0,
    limit: Annotated[int, Query(ge=1, le=50)] = 50,
) -> SourceRestrictionHistory:
    scope = _store(repository, session_cookie, organization_id, write=False)
    try:
        records = TenantSourceRestrictionStore(scope).history(
            queue_sha256, offset=offset, limit=limit + 1,
        )
    except ValueError as error:
        raise ApiError(422, "invalid_source_restriction", "Не удалось определить исходный документ или набор заявок. Выберите данные для проверки заново.") from error
    except TenantAccessDenied as error:
        raise ApiError(403, "organization_forbidden", "У вас нет доступа к истории проверки исходных документов.") from error
    except (RuntimeError, SQLAlchemyError) as error:
        raise ApiError(503, "restriction_store_unavailable", "Не удалось получить или сохранить проверку исходных документов. Проверьте историю решений перед повторной попыткой.") from error
    response.headers["Cache-Control"] = "no-store"
    return SourceRestrictionHistory(
        rows=[SourceRestrictionRow.model_validate(item) for item in records[:limit]],
        next_offset=offset + limit if len(records) > limit else None,
    )
