"""Session-bound PostgreSQL tenant catalogs with isolated live/test layouts.

The live release uses tenant_catalog_active and tenant_catalog_idempotency
through the restricted kpdesk_tenant_runtime PostgreSQL role. A8 isolated
tests retain their original tenant-scoped layout. Authentication comes from
a server-verified session; the legacy singleton is never a live fallback.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import Connection, bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from kpdesk.catalog.schema import CatalogImportResult
from kpdesk.catalog.storage import (
    CatalogImportConflict,
    CatalogStateError,
    InvalidCatalog,
    PublishedCatalog,
)
from kpdesk.db.authenticated_tenant import bind_authenticated_tenant
from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone
from kpdesk.db.tenant_access import (
    AuthenticatedUser,
    TenantAccessDenied,
    TenantOperation,
    authorize_test_tenant_runtime,
)
from kpdesk.db.tenant_table_layout import tenant_table_layout
from kpdesk.matching.profiles import CATEGORY_ATTRIBUTES
from kpdesk.matching.schema import CatalogProduct


class CatalogSchemaIncompatible(ValueError):
    """The canonical catalog contains categories not accepted by PostgreSQL."""

    def __init__(self, categories: tuple[str, ...]) -> None:
        self.categories = categories
        super().__init__("Unsupported database categories: " + ", ".join(categories))


def _database_catalog_categories(connection: Connection) -> frozenset[str]:
    """Inspect the actual database CHECK, never trust only the Python taxonomy.

    A strict local schema mismatch is an availability/validation blocker,
    not a reason to relax database constraints or attempt a partial INSERT.
    """
    definition = connection.scalar(text("""
        SELECT pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE conrelid='public.catalog_items'::regclass
          AND conname='valid_catalog_category' AND contype='c'
    """))
    if definition is None:
        raise RuntimeError("Catalog category DB constraint is absent")
    if not isinstance(definition, str):
        raise TypeError("Catalog category DB constraint is malformed")
    categories = frozenset(re.findall(r"'([a-z_]+)'", definition))
    if not categories or not categories.issubset(CATEGORY_ATTRIBUTES):
        raise RuntimeError("Catalog category DB constraint cannot be verified")
    return categories


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_KEY = re.compile(r"[\x21-\x7e]{1,128}\Z")
_INSERT_ITEM = text("""
    INSERT INTO catalog_items (
        organization_id, catalog_version_id, sku, name, category, sale_unit,
        pieces_per_sale_unit, price_rub, brand, paper_format, paper_gsm,
        pen_ink_color, pen_ink_type, attributes
    ) VALUES (
        :org, :version, :sku, :name, :category, :sale_unit,
        :pieces_per_sale_unit, :price_rub, :brand, :paper_format, :paper_gsm,
        :pen_ink_color, :pen_ink_type, :attributes
    )
""").bindparams(bindparam("attributes", type_=JSONB))


@dataclass(frozen=True, slots=True)
class TenantCatalogRepository:
    """Runs inside an already open transaction; never commits a connection."""

    connection: Connection
    identity: AuthenticatedUser
    organization_id: uuid.UUID
    session_token: str | None = None
    csrf_token: str | None = None

    def _authorize(self, operation: TenantOperation) -> None:
        if not self.connection.in_transaction():
            raise RuntimeError("Tenant repository requires a caller-owned transaction.")
        database, role = self.connection.execute(
            text("SELECT current_database(), current_user")
        ).one()
        if (database == "kpdesk" or is_live_accounts_http_clone(self.connection)) and (
            role != "kpdesk_tenant_runtime"
        ):
            raise RuntimeError("Live tenant access requires restricted tenant role.")
        session_bound_schema = self.connection.scalar(text("""
            SELECT to_regprocedure(
                'public.kpdesk_session_tenant_allowed(uuid,text)'
            ) IS NOT NULL
        """))
        if session_bound_schema:
            if self.session_token is None:
                raise TenantAccessDenied(
                    "A8 tenant authorization requires an active verified session."
                )
            trusted = bind_authenticated_tenant(
                self.connection,
                session_token=self.session_token,
                organization_id=self.organization_id,
                operation=operation,
                csrf=self.csrf_token,
            )
            if trusted.identity.user_id != self.identity.user_id:
                self.connection.execute(text("""
                    SELECT set_config('kpdesk.session_sha256', '', true),
                           set_config('kpdesk.organization_id', '', true)
                """))
                raise TenantAccessDenied("Verified session identity mismatch.")
        else:
            # Legacy A3/A7 test-only contract; never used after A8 release.
            authorize_test_tenant_runtime(
                self.connection, self.identity, self.organization_id, operation,
            )

    def unavailable_categories(
        self, products: tuple[CatalogProduct, ...],
    ) -> tuple[str, ...]:
        self._authorize("write")
        supported = _database_catalog_categories(self.connection)
        return tuple(sorted({item.category for item in products} - supported))

    def current(self) -> PublishedCatalog | None:
        self._authorize("read")
        active_table = tenant_table_layout(self.connection).active_catalog
        row = self.connection.execute(text(f"""
            SELECT v.id, v.source_sha256, v.rows_count
            FROM {active_table} a
            JOIN catalog_versions v
              ON v.organization_id = a.organization_id
             AND v.id = a.catalog_version_id
            WHERE a.organization_id = :org
        """), {"org": self.organization_id}).one_or_none()
        if row is None:
            return None
        return PublishedCatalog(row.id, row.source_sha256, row.rows_count)

    def versions(self, *, limit: int = 100, offset: int = 0) -> list[PublishedCatalog]:
        """List bounded immutable sources scoped to the verified organization."""
        if not 1 <= limit <= 100 or not 0 <= offset <= 10000:
            raise ValueError("invalid_catalog_history_page")
        self._authorize("read")
        rows = self.connection.execute(text("""
            SELECT id,source_sha256,rows_count
            FROM public.catalog_versions
            WHERE organization_id=:org
            ORDER BY created_at DESC,id DESC
            LIMIT :limit OFFSET :offset
        """), {
            "org": self.organization_id, "limit": limit, "offset": offset,
        }).all()
        return [PublishedCatalog(row.id, row.source_sha256, row.rows_count) for row in rows]

    def items(self, version_id: uuid.UUID) -> list[CatalogProduct]:
        self._authorize("read")
        rows = self.connection.execute(text("""
            SELECT sku, name, category, sale_unit, pieces_per_sale_unit,
                   price_rub, brand, paper_format, paper_gsm,
                   pen_ink_color, pen_ink_type, attributes
            FROM catalog_items
            WHERE organization_id = :org AND catalog_version_id = :version
            ORDER BY sku
        """), {"org": self.organization_id, "version": version_id}).mappings().all()
        return [CatalogProduct.model_validate(dict(row)) for row in rows]

    def lock_for_publish(self) -> None:
        """Serialize publications for this organization within caller transaction.

        May be called before obtaining additional review locks. PostgreSQL
        transaction-scoped advisory locks are reentrant in the same connection.
        """
        self._authorize("write")
        lock_key = int.from_bytes(
            hashlib.sha256(self.organization_id.bytes).digest()[:8],
            byteorder="big", signed=True,
        )
        self.connection.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )
        # Membership may be revoked while the transaction waits on the lock.
        self._authorize("write")

    def publish(
        self,
        source: CatalogImportResult,
        idempotency_key: str,
        request_hash: str,
    ) -> PublishedCatalog:
        self._authorize("write")
        if (
            not source.publishable or not source.products
            or not _SHA256.fullmatch(source.source_sha256)
            or source.source_sha256 != request_hash
        ):
            raise InvalidCatalog("Catalog validation and evidence are required.")
        if not _KEY.fullmatch(idempotency_key):
            raise InvalidCatalog("Invalid idempotency key.")
        unavailable = self.unavailable_categories(source.products)
        if unavailable:
            raise CatalogSchemaIncompatible(unavailable)
        self.lock_for_publish()
        tables = tenant_table_layout(self.connection)
        existing = self.connection.execute(text(f"""
            SELECT request_sha256, result_version_id
            FROM {tables.catalog_idempotency}
            WHERE organization_id = :org AND namespace = 'catalog_import' AND key = :key
        """), {"org": self.organization_id, "key": idempotency_key}).one_or_none()
        if existing is not None:
            if existing.request_sha256 != request_hash:
                raise CatalogImportConflict("Idempotency key used for a different file.")
            version = self.connection.execute(text("""
                SELECT id, source_sha256, rows_count FROM catalog_versions
                WHERE organization_id = :org AND id = :version
            """), {
                "org": self.organization_id, "version": existing.result_version_id,
            }).one_or_none()
            if version is None:
                raise CatalogStateError("Broken tenant catalog idempotency reference.")
            return PublishedCatalog(version.id, version.source_sha256, version.rows_count)

        version_id = uuid.uuid4()
        self.connection.execute(text("""
            INSERT INTO catalog_versions (
                id, organization_id, source_sha256, original_filename, rows_count
            ) VALUES (:id, :org, :hash, :name, :count)
        """), {
            "id": version_id, "org": self.organization_id,
            "hash": source.source_sha256,
            "name": source.original_filename,
            "count": len(source.products),
        })
        for product in source.products:
            self.connection.execute(_INSERT_ITEM, {
                "org": self.organization_id, "version": version_id,
                **product.model_dump(),
            })
        self.connection.execute(text(f"""
            INSERT INTO {tables.active_catalog} (organization_id, catalog_version_id)
            VALUES (:org, :version)
            ON CONFLICT (organization_id)
            DO UPDATE SET catalog_version_id = EXCLUDED.catalog_version_id
        """), {"org": self.organization_id, "version": version_id})
        self.connection.execute(text(f"""
            INSERT INTO {tables.catalog_idempotency} (
                organization_id, namespace, key, request_sha256, result_version_id
            ) VALUES (:org, 'catalog_import', :key, :hash, :version)
        """), {
            "org": self.organization_id, "key": idempotency_key,
            "hash": source.source_sha256, "version": version_id,
        })
        return PublishedCatalog(version_id, source.source_sha256, len(source.products))
