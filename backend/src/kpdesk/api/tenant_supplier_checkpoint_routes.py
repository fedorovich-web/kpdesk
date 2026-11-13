"""A73 clone-only HTTP checkpoint exercise. Not a production persistence API.

Mounted solely by create_app(testing=True, tenant_sessions=True). The SQL
functions are installed only by the disposable clone harness, never Alembic.
Every saved/retrieved decision is checked against re-uploaded source bytes.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import replace
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Cookie, Depends, File, Form, Header, Request, Response, UploadFile
from pydantic import BaseModel, Field, RootModel, ValidationError
from sqlalchemy import Connection, bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from kpdesk.api.errors import ApiError
from kpdesk.api.supplier_routes import MappingBody, _safe_uploaded_source
from kpdesk.api.tenant_session_routes import SESSION_COOKIE, get_unreleased_auth_repository
from kpdesk.api.tenant_supplier_canonical_routes import (
    CanonicalExportBody,
    ExcludeBody,
    IncludeBody,
    reviewed_decisions,
)
from kpdesk.api.tenant_supplier_precision_routes import _authorized_supplier_write
from kpdesk.catalog.supplier_canonical_preview import (
    CanonicalExportRejected,
    ReviewedExclusion,
    preview_canonical_catalog,
    source_row_sha256,
)
from kpdesk.catalog.supplier_profiles import PriceBasis
from kpdesk.catalog.supplier_staging import (
    SupplierMapping,
    SupplierStaging,
    mapping_digest,
    stage_supplier_prices,
)
from kpdesk.db.auth_sessions import AuthSessionRepository
from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone
from kpdesk.db.tenant_access import AuthenticatedUser
from kpdesk.matching.profiles import TAXONOMY_VERSION

router = APIRouter(
    prefix="/api/session/organizations/{organization_id}/suppliers/{supplier_id}/canonical/checkpoints",
)
_KEY = re.compile(r"[A-Za-z0-9_.-]{8,128}\Z")
_PUT = text("""
    SELECT public.a72_checkpoint_put(
        :org,:supplier,:source,:mapping,:taxonomy,:basis,
        :idempotency,:expected,:decisions
    )
""").bindparams(bindparam("decisions", type_=JSONB))
_GET = text("""
    SELECT checkpoint_id,reviewer_user_id,previous_id,taxonomy_version,
           price_basis,decisions
    FROM public.a72_checkpoint_get(:org,:supplier,:source,:mapping,:taxonomy,:basis)
""")


class SaveBody(CanonicalExportBody):
    idempotency_key: str = Field(min_length=8, max_length=128)
    expected_checkpoint_id: uuid.UUID | None = None
    decisions: list[IncludeBody | ExcludeBody] = Field(min_length=1, max_length=50)


class RestoreBody(BaseModel):
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    mapping: MappingBody
    formula_policy: Literal["reject", "quarantine_rows"] = "reject"
    price_basis: PriceBasis


class CheckpointResult(BaseModel):
    checkpoint_id: uuid.UUID
    source_sha256: str
    mapping_sha256: str
    taxonomy_version: str
    price_basis: PriceBasis
    reviewer_user_id: uuid.UUID
    previous_id: uuid.UUID | None
    decisions: list[IncludeBody | ExcludeBody]
    published: bool = False


class SavedCheckpoint(BaseModel):
    checkpoint_id: uuid.UUID
    source_sha256: str
    mapping_sha256: str
    taxonomy_version: str
    recorded: bool = True
    published: bool = False


def _staged_source(
    request: Request, upload: UploadFile, *, source_sha256: str,
    mapping: MappingBody, formula_policy: Literal["reject", "quarantine_rows"],
) -> tuple[SupplierStaging, str]:
    if mapping.source_sha256 not in (None, source_sha256):
        raise ApiError(422, "invalid_mapping", "Настройки колонок относятся к другому файлу. Выберите исходный прайс или сопоставьте колонки заново.")
    try:
        parsed = SupplierMapping(**mapping.model_dump(exclude={"source_sha256"}))
    except (ValueError, TypeError) as error:
        raise ApiError(422, "invalid_mapping", "Настройки колонок не прошли проверку. Сопоставьте поля с исходным прайсом заново.") from error
    filename, content = _safe_uploaded_source(request, upload)
    if not filename.casefold().endswith((".csv", ".xls", ".xlsx")):
        raise ApiError(422, "invalid_supplier_source", "Этот формат файла недоступен на данном шаге. Выберите прайс поддерживаемого формата.")
    if hashlib.sha256(content).hexdigest() != source_sha256:
        raise ApiError(409, "source_changed", "Выберите тот же исходный файл прайса, для которого сохранены решения.")
    try:
        staged = stage_supplier_prices(filename, content, parsed, formula_policy=formula_policy)
    except (ValueError, TypeError) as error:
        raise ApiError(422, "invalid_supplier_source", "Не удалось разобрать строки прайса. Проверьте исходный файл и настройки колонок.") from error
    return staged, mapping_digest(parsed, formula_policy)


def _check_partial(
    staging: SupplierStaging, records: list[IncludeBody | ExcludeBody],
    *, org: uuid.UUID, reviewer: uuid.UUID, price_basis: PriceBasis,
) -> None:
    rows = {row.line: row for row in staging.rows}
    if len(records) > 50 or len({item.line for item in records}) != len(records):
        raise ApiError(422, "duplicate_review_decision", "Для каждой строки допустимо одно решение. Проверьте повторы и ограничение в 50 решений за один запрос.")
    chosen = []
    for item in records:
        row = rows.get(item.line)
        if row is None or item.source_row_sha256 != source_row_sha256(row):
            raise ApiError(409, "source_row_changed", "Исходная строка изменилась или больше не найдена. Заново проверьте строку и принятое решение.", row=item.line)
        chosen.append(row)
    decisions = reviewed_decisions(records)
    if not any(not isinstance(item, ReviewedExclusion) for item in decisions):
        # Exclusion-only drafts do not form a publishable catalog. Their hashes,
        # explicit confirmations and reasons still need validation.
        return
    selection = replace(staging, rows=tuple(chosen))
    try:
        preview_canonical_catalog(
            selection, decisions, organization_id=org,
            reviewer=AuthenticatedUser(reviewer),
            expected_source_sha256=staging.sha256,
            price_basis=price_basis, explicitly_confirmed_price_basis=True,
        )
    except CanonicalExportRejected as error:
        raise ApiError(422, error.code, "Решения по товарам не прошли проверку для каталога. Проверьте параметры строк и подтверждения.",
                       row=error.line) from error


def _scope(connection: Connection) -> None:
    # Not exposed to the release: only the provenance-marked clone may use A72 SQL.
    if not is_live_accounts_http_clone(connection) or connection.scalar(
        text("SELECT current_user")
    ) != "kpdesk_tenant_runtime":
        raise ApiError(503, "checkpoint_unavailable", "Сохранение проверок прайса сейчас недоступно. Обратитесь к администратору KPDesk.")
    if connection.scalar(text("""
        SELECT to_regprocedure(
          'public.a72_checkpoint_put(uuid,text,text,text,text,text,text,uuid,jsonb)'
        ) IS NOT NULL
        AND to_regprocedure(
          'public.a72_checkpoint_get(uuid,text,text,text,text,text)'
        ) IS NOT NULL
    """)) is not True:
        raise ApiError(503, "checkpoint_unavailable", "Сохранение проверок прайса сейчас недоступно. Обратитесь к администратору KPDesk.")


def _decode_body[T: SaveBody | RestoreBody](raw: str, schema: type[T]) -> T:
    if len(raw) > 100_000:
        raise ApiError(413, "review_too_large", "Данные проверки превышают допустимый объём. Сократите число решений в запросе.")
    try:
        return cast(T, schema.model_validate_json(raw))
    except ValidationError as error:
        raise ApiError(422, "invalid_canonical_review", "Данные проверки не приняты. Проверьте решения по строкам и обязательные поля.") from error


@router.post("/save", response_model=SavedCheckpoint, status_code=201)
def save(
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
    authorized = _authorized_supplier_write(
        repository, session_cookie, organization_id, supplier_id, csrf,
    )
    _scope(repository.connection)
    body = _decode_body(review_json, SaveBody)
    if not _KEY.fullmatch(body.idempotency_key):
        raise ApiError(422, "invalid_idempotency_key", "Не удалось определить запрос сохранения. Проверьте сохранённые решения перед повторной попыткой.")
    staged, digest = _staged_source(
        request, upload, source_sha256=body.source_sha256,
        mapping=body.mapping, formula_policy=body.formula_policy,
    )
    _check_partial(staged, body.decisions, org=organization_id,
                   reviewer=authorized.identity.user_id, price_basis=body.price_basis)
    # A74 fail-closed draft continuity: the A72 prototype stores a whole
    # snapshot of at most 50 rows, not an incremental multi-page delta.
    # A later revision must not silently discard previously reviewed rows.
    # The SQL optimistic lock below still resolves concurrent writes.
    if body.expected_checkpoint_id is not None:
        selector = {
            "org": organization_id, "supplier": supplier_id,
            "source": staged.sha256, "mapping": digest,
            "taxonomy": TAXONOMY_VERSION, "basis": body.price_basis,
        }
        try:
            prior = repository.connection.execute(_GET, selector).one_or_none()
        except SQLAlchemyError as error:
            raise ApiError(503, "checkpoint_unavailable", "Не удалось загрузить сохранённые решения по прайсу. Попробуйте позже.") from error
        if prior is not None and prior.checkpoint_id == body.expected_checkpoint_id:
            previous_lines = {entry["line"] for entry in prior.decisions}
            current_lines = {entry.line for entry in body.decisions}
            if not previous_lines.issubset(current_lines):
                raise ApiError(
                    409, "checkpoint_dropped_rows",
                    "В новой версии проверки должны остаться все ранее подтверждённые строки. Загрузите сохранённые решения и проверьте состав.",
                )
    args = {
        "org": organization_id, "supplier": supplier_id,
        "source": staged.sha256, "mapping": digest,
        "taxonomy": TAXONOMY_VERSION, "basis": body.price_basis,
        "idempotency": body.idempotency_key,
        "expected": body.expected_checkpoint_id,
        "decisions": [item.model_dump(exclude_none=True) for item in body.decisions],
    }
    try:
        saved = repository.connection.scalar(_PUT, args)
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) in ("23505", "40001"):
            raise ApiError(409, "checkpoint_conflict", "Проверка уже изменена другим запросом. Загрузите актуальные решения перед сохранением.") from error
        raise ApiError(503, "checkpoint_unavailable", "Не удалось подтвердить сохранение проверки. Загрузите сохранённые решения и проверьте результат перед повторной попыткой.") from error
    if not isinstance(saved, uuid.UUID):
        raise ApiError(403, "checkpoint_forbidden", "Не удалось подтвердить доступ к сохранённой проверке прайса.")
    response.headers["Cache-Control"] = "no-store"
    return SavedCheckpoint(
        checkpoint_id=saved, source_sha256=staged.sha256, mapping_sha256=digest,
        taxonomy_version=TAXONOMY_VERSION,
    )


@router.post("/restore", response_model=CheckpointResult | None)
def restore(
    organization_id: uuid.UUID, supplier_id: str, request: Request,
    response: Response,
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    upload: Annotated[UploadFile, File(alias="file")],
    review_json: Annotated[str, Form()],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> CheckpointResult | None:
    authorized = _authorized_supplier_write(
        repository, session_cookie, organization_id, supplier_id, csrf,
    )
    _scope(repository.connection)
    body = _decode_body(review_json, RestoreBody)
    staged, digest = _staged_source(
        request, upload, source_sha256=body.source_sha256,
        mapping=body.mapping, formula_policy=body.formula_policy,
    )
    try:
        row = repository.connection.execute(_GET, {
            "org": organization_id, "supplier": supplier_id, "source": staged.sha256,
            "mapping": digest, "taxonomy": TAXONOMY_VERSION, "basis": body.price_basis,
        }).one_or_none()
    except SQLAlchemyError as error:
        raise ApiError(503, "checkpoint_unavailable", "Не удалось загрузить сохранённые решения по прайсу. Попробуйте позже.") from error
    response.headers["Cache-Control"] = "no-store"
    if row is None:
        return None
    try:
        decisions = [SaveDecision.model_validate(item).root for item in row.decisions]
    except (ValidationError, TypeError, ValueError) as error:
        raise ApiError(409, "checkpoint_invalid", "Не удалось подтвердить целостность сохранённых решений. Сверьте исходный файл и проверьте строки заново.") from error
    _check_partial(staged, decisions, org=organization_id,
                   reviewer=authorized.identity.user_id, price_basis=body.price_basis)
    return CheckpointResult(
        checkpoint_id=row.checkpoint_id,
        source_sha256=staged.sha256, mapping_sha256=digest,
        taxonomy_version=TAXONOMY_VERSION, price_basis=body.price_basis,
        reviewer_user_id=row.reviewer_user_id, previous_id=row.previous_id,
        decisions=decisions,
    )


class SaveDecision(RootModel[IncludeBody | ExcludeBody]):
    pass
