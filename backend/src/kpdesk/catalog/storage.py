"""Catalog versions are published in a single transaction under a row lock."""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from kpdesk.catalog.schema import CatalogImportResult
from kpdesk.db.models import CatalogActive, CatalogItem, CatalogVersion, IdempotencyRecord
from kpdesk.matching.schema import CatalogProduct


@dataclass(frozen=True, slots=True)
class PublishedCatalog:
    id: uuid.UUID
    source_sha256: str
    rows_count: int


def _snapshot(version: CatalogVersion) -> PublishedCatalog:
    return PublishedCatalog(
        id=version.id,
        source_sha256=version.source_sha256,
        rows_count=version.rows_count,
    )


class InvalidCatalog(ValueError):
    pass


class CatalogImportConflict(ValueError):
    pass


class CatalogStateError(RuntimeError):
    pass


def get_active_catalog(session: Session) -> CatalogVersion | None:
    active = session.get(CatalogActive, 1)
    if active is None or active.catalog_version_id is None:
        return None
    return session.get(CatalogVersion, active.catalog_version_id)


def list_catalog_items(session: Session, version_id: uuid.UUID) -> list[CatalogProduct]:
    rows = session.scalars(
        select(CatalogItem)
        .where(CatalogItem.catalog_version_id == version_id)
        .order_by(CatalogItem.sku)
    ).all()
    return [
        CatalogProduct.model_validate({
            "sku": row.sku,
            "name": row.name,
            "category": row.category,
            "sale_unit": row.sale_unit,
            "pieces_per_sale_unit": row.pieces_per_sale_unit,
            "price_rub": row.price_rub,
            "brand": row.brand,
            "paper_format": row.paper_format,
            "paper_gsm": row.paper_gsm,
            "pen_ink_color": row.pen_ink_color,
            "pen_ink_type": row.pen_ink_type,
            "attributes": row.attributes,
        })
        for row in rows
    ]


def publish_catalog(
    session: Session,
    source: CatalogImportResult,
    idempotency_key: str,
    request_hash: str,
) -> PublishedCatalog:
    if not source.publishable:
        raise InvalidCatalog("Каталог содержит ошибки и не может быть опубликован.")
    if request_hash != source.source_sha256:
        raise InvalidCatalog("Хеш запроса не совпадает с содержимым прайса.")
    if not 1 <= len(idempotency_key) <= 128:
        raise InvalidCatalog("Idempotency-Key должен содержать 1–128 символов.")

    with session.begin():
        active = session.get(CatalogActive, 1, with_for_update=True)
        if active is None:
            raise CatalogStateError("Миграция не создала активный указатель каталога.")

        existing = session.get(IdempotencyRecord, ("catalog_import", idempotency_key))
        if existing is not None:
            if existing.request_sha256 != request_hash:
                raise CatalogImportConflict("Ключ идемпотентности использован для другого файла.")
            result = session.get(CatalogVersion, existing.result_version_id)
            if result is None:
                raise CatalogStateError("Повреждена запись идемпотентности.")
            return _snapshot(result)

        version = CatalogVersion(
            source_sha256=source.source_sha256,
            original_filename=source.original_filename,
            rows_count=len(source.products),
        )
        session.add(version)
        session.flush()
        session.add_all([
            CatalogItem(
                catalog_version_id=version.id,
                **product.model_dump(),
            )
            for product in source.products
        ])
        active.catalog_version_id = version.id
        session.add(IdempotencyRecord(
            namespace="catalog_import",
            key=idempotency_key,
            request_sha256=request_hash,
            result_version_id=version.id,
        ))
        session.flush()
        return _snapshot(version)
