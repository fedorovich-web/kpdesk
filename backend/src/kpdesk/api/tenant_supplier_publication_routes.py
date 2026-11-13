"""A82 isolated-clone-only HTTPS publication from immutable human review.

Mounted only by create_app(testing=True, tenant_sessions=True). The normal
tenant application has no route and no production checkpoint schema.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, File, Form, Header, Request, Response, UploadFile
from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError

from kpdesk.api.errors import ApiError
from kpdesk.api.supplier_routes import _safe_uploaded_source
from kpdesk.api.tenant_session_routes import (
    SESSION_COOKIE,
    CurrentTenantCatalogResponse,
    get_unreleased_auth_repository,
)
from kpdesk.api.tenant_supplier_checkpoint_routes import RestoreBody, _decode_body, _scope
from kpdesk.api.tenant_supplier_precision_routes import _authorized_supplier_write
from kpdesk.catalog.review_signing import ReviewSigningCredential
from kpdesk.catalog.review_worker_admission import (
    ReviewWorkerPublicationGate,
)
from kpdesk.catalog.reviewed_publication import (
    VerifiedReviewPublishRejected,
    publish_verified_supplier_review,
)
from kpdesk.catalog.storage import CatalogImportConflict, InvalidCatalog
from kpdesk.catalog.supplier_staging import SupplierMapping
from kpdesk.db.auth_sessions import AuthSessionRepository
from kpdesk.db.tenant_access import TenantAccessDenied


def get_review_publish_signer(request: Request) -> Iterator[ReviewSigningCredential | None]:
    """Hold worker admission through the FastAPI per-request DB COMMIT.

    This dependency MUST execute before the transaction dependency so that
    its finalizer runs after the repository commits or rolls back.
    """
    gate = getattr(request.app.state, "a94_review_worker_gate", None)
    if isinstance(gate, ReviewWorkerPublicationGate):
        with gate.optional_permit() as signer:
            yield signer
    elif gate is None:
        legacy_signer = getattr(request.app.state, "a85_review_signing_credential", None)
        yield legacy_signer if isinstance(legacy_signer, ReviewSigningCredential) else None
    else:
        yield None


router = APIRouter(
    prefix="/api/session/organizations/{organization_id}/suppliers/"
           "{supplier_id}/canonical/checkpoints",
)
_KEY = re.compile(r"[A-Za-z0-9_.-]{8,128}\Z")


@router.post("/{checkpoint_id}/publish", status_code=201,
             response_model=CurrentTenantCatalogResponse)
def publish_reviewed_checkpoint(
    organization_id: uuid.UUID,
    supplier_id: str,
    checkpoint_id: uuid.UUID,
    request: Request,
    response: Response,
    signing_credential: Annotated[
        ReviewSigningCredential | None, Depends(get_review_publish_signer, scope="function"),
    ],
    repository: Annotated[
        AuthSessionRepository, Depends(get_unreleased_auth_repository, scope="function"),
    ],
    upload: Annotated[UploadFile, File(alias="file")],
    review_json: Annotated[str, Form()],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    canonical_sha256: Annotated[str | None, Header(alias="X-Catalog-SHA256")] = None,
) -> CurrentTenantCatalogResponse:
    authorized = _authorized_supplier_write(
        repository, session_cookie, organization_id, supplier_id, csrf,
    )
    if not isinstance(signing_credential, ReviewSigningCredential):
        raise ApiError(503, "review_signer_unavailable", "Подтверждение проверки перед загрузкой прайса сейчас недоступно. Обратитесь к администратору KPDesk.")
    # The per-clone key is provisioned only to the trusted test server.
    # Neither tenant credentials nor request fields may supply this key.
    _scope(repository.connection)
    if idempotency_key is None or _KEY.fullmatch(idempotency_key) is None:
        raise ApiError(422, "invalid_idempotency_key", "Не удалось определить запрос загрузки прайса. Проверьте активную версию перед повторной попыткой.")
    if canonical_sha256 is None or re.fullmatch(r"[0-9a-f]{64}", canonical_sha256) is None:
        raise ApiError(422, "invalid_canonical_hash", "Сначала подготовьте и проверьте файл каталога, затем повторите загрузку.")
    body = _decode_body(review_json, RestoreBody)
    if body.mapping.source_sha256 not in (None, body.source_sha256):
        raise ApiError(422, "invalid_mapping", "Настройки колонок относятся к другому файлу. Выберите исходный прайс или сопоставьте колонки заново.")
    try:
        mapping = SupplierMapping(**body.mapping.model_dump(exclude={"source_sha256"}))
    except (ValueError, TypeError, ValidationError) as error:
        raise ApiError(422, "invalid_mapping", "Настройки исходного прайса не прошли проверку. Заново сопоставьте колонки и подтвердите данные.") from error
    filename, raw = _safe_uploaded_source(request, upload)
    if hashlib.sha256(raw).hexdigest() != body.source_sha256:
        raise ApiError(409, "source_changed", "Исходный прайс изменился. Выберите файл, для которого выполнялась проверка, или проверьте новый заново.")
    try:
        published = publish_verified_supplier_review(
            repository.connection,
            organization_id=organization_id,
            identity=authorized.identity,
            session_token=session_cookie or "",
            csrf_token=csrf or "",
            supplier_id=supplier_id,
            checkpoint_id=checkpoint_id,
            source_filename=filename,
            source_bytes=raw,
            mapping=mapping,
            formula_policy=body.formula_policy,
            price_basis=body.price_basis,
            expected_canonical_sha256=canonical_sha256,
            idempotency_key=idempotency_key,
            signing_credential=signing_credential,
        )
    except TenantAccessDenied as error:
        raise ApiError(403, "organization_forbidden", "У вас нет доступа к данным этой организации.") from error
    except CatalogImportConflict as error:
        raise ApiError(409, "catalog_import_conflict", "Не удалось подтвердить повторную загрузку прайса. Проверьте активную версию перед новой попыткой.") from error
    except (VerifiedReviewPublishRejected, InvalidCatalog) as error:
        raise ApiError(409, "review_publish_conflict", "Не удалось подтвердить готовность прайса к загрузке. Заново проверьте исходный файл и решения по товарам.") from error
    except DBAPIError as error:
        raise ApiError(503, "review_publish_unavailable", "Не удалось подтвердить загрузку проверенного прайса. Проверьте активную версию перед повторной попыткой.") from error
    response.headers["Cache-Control"] = "no-store"
    return CurrentTenantCatalogResponse(
        version_id=published.id,
        source_sha256=published.source_sha256,
        rows_count=published.rows_count,
    )
