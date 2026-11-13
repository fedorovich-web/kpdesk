"""A75 paged canonical review exercise; mounted only on isolated testing tenant app.

Each checkpoint contains <=50 individually confirmed source-bound decisions.
The A72 SQL optimistic lock serializes append; A75 SQL reconstructs the latest
per-line decision across all matching immutable checkpoint revisions.
"""
from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Depends, File, Form, Header, Request, Response, UploadFile
from pydantic import Field
from sqlalchemy import Connection, text
from sqlalchemy.engine import Row
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from kpdesk.api.errors import ApiError
from kpdesk.api.tenant_session_routes import SESSION_COOKIE, get_unreleased_auth_repository
from kpdesk.api.tenant_supplier_canonical_routes import ExcludeBody, IncludeBody
from kpdesk.api.tenant_supplier_checkpoint_routes import (
    _KEY,
    _PUT,
    RestoreBody,
    SaveBody,
    SavedCheckpoint,
    SaveDecision,
    _check_partial,
    _decode_body,
    _scope,
    _staged_source,
)
from kpdesk.api.tenant_supplier_precision_routes import _authorized_supplier_write
from kpdesk.db.auth_sessions import AuthSessionRepository
from kpdesk.matching.profiles import TAXONOMY_VERSION

router = APIRouter(
    prefix="/api/session/organizations/{organization_id}/suppliers/{supplier_id}/canonical/checkpoints",
)
_PAGE = text("""
    SELECT checkpoint_id,reviewer_user_id,previous_id,taxonomy_version,
           price_basis,total_count,decisions
    FROM public.a77_checkpoint_page(
      :org,:supplier,:source,:mapping,:taxonomy,:basis,:offset,:limit
    )
""")
_PROBE = text("""
    SELECT checkpoint_id,total_count,overlap_count
    FROM public.a77_checkpoint_probe(
      :org,:supplier,:source,:mapping,:taxonomy,:basis,:lines
    )
""")


class AppendBody(SaveBody):
    replace_existing: bool = False


class RestorePageBody(RestoreBody):
    offset: int = Field(default=0, ge=0, le=5000)
    limit: int = Field(default=50, ge=1, le=50)
    expected_checkpoint_id: uuid.UUID | None = None


class RestoredPage(RestorePageBody):
    checkpoint_id: uuid.UUID
    reviewer_user_id: uuid.UUID
    previous_id: uuid.UUID | None
    mapping_sha256: str
    taxonomy_version: str
    total_count: int
    next_offset: int | None
    decisions: list[IncludeBody | ExcludeBody]
    published: bool = False


def _available(connection: Connection) -> None:
    _scope(connection)
    if connection.scalar(text("""
        SELECT to_regprocedure(
          'public.a77_checkpoint_page(uuid,text,text,text,text,text,integer,integer)'
        ) IS NOT NULL
    """)) is not True:
        raise ApiError(503, "checkpoint_unavailable", "Сохранение проверки по частям сейчас недоступно. Обратитесь к администратору KPDesk.")
    if connection.scalar(text("""
        SELECT to_regprocedure(
          'public.a77_checkpoint_probe(uuid,text,text,text,text,text,integer[])'
        ) IS NOT NULL
    """)) is not True:
        raise ApiError(503, "checkpoint_unavailable", "Проверка сохранённых решений сейчас недоступна. Обратитесь к администратору KPDesk.")


def _page(
    connection: Connection, selector: dict[str, object], *, offset: int, limit: int,
) -> Row[Any] | None:
    try:
        return connection.execute(
            _PAGE, {**selector, "offset": offset, "limit": limit},
        ).one_or_none()
    except SQLAlchemyError as error:
        raise ApiError(503, "checkpoint_unavailable", "Не удалось загрузить сохранённые решения по прайсу. Попробуйте позже.") from error


def _probe(
    connection: Connection, selector: dict[str, object], lines: list[int],
) -> Row[Any] | None:
    try:
        return connection.execute(_PROBE, {**selector, "lines": lines}).one_or_none()
    except SQLAlchemyError as error:
        raise ApiError(503, "checkpoint_unavailable", "Не удалось проверить актуальность сохранённых решений. Загрузите их заново перед сохранением.") from error


@router.post("/append", status_code=201, response_model=SavedCheckpoint)
def append_review(
    organization_id: uuid.UUID, supplier_id: str, request: Request,
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    upload: Annotated[UploadFile, File(alias="file")],
    review_json: Annotated[str, Form()],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> SavedCheckpoint:
    actor = _authorized_supplier_write(
        repository, session_cookie, organization_id, supplier_id, csrf,
    )
    connection = repository.connection
    _available(connection)
    body = _decode_body(review_json, AppendBody)
    if not _KEY.fullmatch(body.idempotency_key):
        raise ApiError(422, "invalid_idempotency_key", "Не удалось определить запрос сохранения. Проверьте сохранённые решения перед повторной попыткой.")
    staged, digest = _staged_source(
        request, upload, source_sha256=body.source_sha256, mapping=body.mapping,
        formula_policy=body.formula_policy,
    )
    _check_partial(
        staged, body.decisions, org=organization_id,
        reviewer=actor.identity.user_id, price_basis=body.price_basis,
    )
    selector: dict[str, object] = {
        "org": organization_id, "supplier": supplier_id,
        "source": staged.sha256, "mapping": digest,
        "taxonomy": TAXONOMY_VERSION, "basis": body.price_basis,
    }
    # One source-bound, RLS-checked SQL probe replaces the previous O(pages)
    # repeated reconstruction of the entire immutable revision chain.
    incoming_lines = [decision.line for decision in body.decisions]
    head = _probe(connection, selector, incoming_lines)
    if head is None and body.expected_checkpoint_id is not None:
        raise ApiError(409, "checkpoint_conflict", "Ожидаемая сохранённая версия проверки не найдена. Загрузите актуальные решения перед сохранением.")
    if head is not None and head.checkpoint_id == body.expected_checkpoint_id:
        if head.total_count + len(incoming_lines) - head.overlap_count > 5000:
            raise ApiError(413, "checkpoint_limit", "Сохранённая проверка не может содержать более 5000 строк.")
        if head.overlap_count and not body.replace_existing:
            raise ApiError(
                409, "checkpoint_existing_rows",
                "В этих строках уже есть проверенные решения. Подтвердите их замену перед сохранением.",
            )

    arguments = {
        **selector, "expected": body.expected_checkpoint_id,
        "idempotency": body.idempotency_key,
        "decisions": [item.model_dump(exclude_none=True) for item in body.decisions],
    }
    try:
        saved = connection.scalar(_PUT, arguments)
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) in ("23505", "40001"):
            raise ApiError(
                409, "checkpoint_conflict", "Проверка уже изменена другим запросом. Загрузите актуальные решения перед сохранением.",
            ) from error
        raise ApiError(503, "checkpoint_unavailable", "Не удалось подтвердить сохранение проверки. Загрузите сохранённые решения и проверьте результат перед повторной попыткой.") from error
    if not isinstance(saved, uuid.UUID):
        raise ApiError(403, "checkpoint_forbidden", "Не удалось подтвердить доступ к сохранённой проверке прайса.")
    response.headers["Cache-Control"] = "no-store"
    return SavedCheckpoint(
        checkpoint_id=saved, source_sha256=staged.sha256,
        mapping_sha256=digest, taxonomy_version=TAXONOMY_VERSION,
    )


@router.post("/restore-page", response_model=RestoredPage | None)
def restore_page(
    organization_id: uuid.UUID, supplier_id: str, request: Request,
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    upload: Annotated[UploadFile, File(alias="file")],
    review_json: Annotated[str, Form()],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> RestoredPage | None:
    actor = _authorized_supplier_write(
        repository, session_cookie, organization_id, supplier_id, csrf,
    )
    connection = repository.connection
    _available(connection)
    body = _decode_body(review_json, RestorePageBody)
    if body.offset > 0 and body.expected_checkpoint_id is None:
        raise ApiError(
            422, "checkpoint_version_required",
            "Не удалось определить версию проверки для следующей страницы. Загрузите сохранённые решения с начала.",
        )
    staged, digest = _staged_source(
        request, upload, source_sha256=body.source_sha256, mapping=body.mapping,
        formula_policy=body.formula_policy,
    )
    selector: dict[str, object] = {
        "org": organization_id, "supplier": supplier_id,
        "source": staged.sha256, "mapping": digest,
        "taxonomy": TAXONOMY_VERSION, "basis": body.price_basis,
    }
    result = _page(connection, selector, offset=body.offset, limit=body.limit)
    response.headers["Cache-Control"] = "no-store"
    if result is None:
        return None
    if body.expected_checkpoint_id is not None and (
        result.checkpoint_id != body.expected_checkpoint_id
    ):
        raise ApiError(
            409, "checkpoint_changed", "Сохранённые решения изменились во время загрузки. Загрузите проверку заново с начала.",
        )
    try:
        decisions = [SaveDecision.model_validate(item).root for item in result.decisions]
    except (ValueError, TypeError) as error:
        raise ApiError(409, "checkpoint_invalid", "Не удалось подтвердить целостность сохранённых решений. Сверьте исходный файл и проверьте строки заново.") from error
    if decisions:
        _check_partial(
            staged, decisions, org=organization_id,
            reviewer=actor.identity.user_id, price_basis=body.price_basis,
        )
    next_offset = body.offset + len(decisions)
    return RestoredPage(
        checkpoint_id=result.checkpoint_id,
        reviewer_user_id=result.reviewer_user_id,
        previous_id=result.previous_id,
        source_sha256=staged.sha256,
        mapping=body.mapping,
        formula_policy=body.formula_policy,
        price_basis=body.price_basis,
        mapping_sha256=digest, taxonomy_version=TAXONOMY_VERSION,
        total_count=result.total_count,
        offset=body.offset, limit=body.limit,
        expected_checkpoint_id=result.checkpoint_id,
        next_offset=next_offset if next_offset < result.total_count else None,
        decisions=decisions,
    )
