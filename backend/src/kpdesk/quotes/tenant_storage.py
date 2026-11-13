"""Session-bound tenant quote revisions for live and A8 isolated PostgreSQL.

The caller owns each transaction. The live layout stores idempotency separately
per organization while keeping immutable historical quotes and shared pricing
logic. The legacy global quote route is never a fallback for this repository.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Connection, bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from kpdesk.catalog.tenant_storage import TenantCatalogRepository
from kpdesk.catalog.verified_supplier_sources import (
    SupplierSourceRef,
    load_reviewed_supplier_source,
)
from kpdesk.company.storage import CompanyProfileStore
from kpdesk.db.authenticated_tenant import bind_authenticated_tenant
from kpdesk.db.tenant_access import AuthenticatedUser, TenantAccessDenied
from kpdesk.db.tenant_table_layout import tenant_table_layout
from kpdesk.extraction.schema import ExtractedRequest
from kpdesk.quotes.alternatives import available_alternatives, resolve_alternatives
from kpdesk.quotes.document import (
    AlternativeSelection,
    CommercialDocument,
    DocumentAlternative,
    DocumentApproval,
)
from kpdesk.quotes.instructions import ConfirmedInstructions
from kpdesk.quotes.storage import (
    ManagerSkuSelection,
    QuoteConflict,
    QuoteNotFound,
    QuoteSnapshot,
    StoredQuote,
    _digest,
    _hash_request,
    _make_snapshot,
    _validate_key,
)
from kpdesk.quotes.supplier_comparison import (
    SupplierComparison,
    SupplierOfferSelection,
    compare_supplier_catalogs,
    lock_supplier_selection,
)

_INSERT_SNAPSHOT = text("""
    INSERT INTO quote_revisions(
        organization_id, quote_id, number, catalog_version_id,
        snapshot, snapshot_sha256, is_complete, total_rub
    ) VALUES (
        :org, :quote, :number, :catalog,
        :snapshot, :sha, :ready, :total
    )
""").bindparams(bindparam("snapshot", type_=JSONB))


@dataclass(frozen=True, slots=True)
class TenantQuoteRepository:
    """One PostgreSQL transaction; every action revalidates session + tenant."""

    connection: Connection
    identity: AuthenticatedUser
    organization_id: uuid.UUID
    session_token: str
    csrf_token: str | None = None

    def _authorize(self, *, write: bool) -> None:
        granted = bind_authenticated_tenant(
            self.connection,
            session_token=self.session_token,
            organization_id=self.organization_id,
            operation="write" if write else "read",
            csrf=self.csrf_token if write else None,
        )
        if granted.identity.user_id != self.identity.user_id:
            self.connection.execute(text("""
                SELECT set_config('kpdesk.session_sha256', '', true),
                       set_config('kpdesk.organization_id', '', true)
            """))
            raise TenantAccessDenied("Quote session identity mismatch.")

    def _lock_tenant(self) -> None:
        """Serialize catalog replacement, first quote create, and revisions.

        Uses the SAME transaction-level mutex as TenantCatalogRepository:
        no split-brain race between an import and price-locked quote creation.
        """
        lock_key = int.from_bytes(
            hashlib.sha256(self.organization_id.bytes).digest()[:8],
            byteorder="big", signed=True,
        )
        self.connection.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key},
        )
        # Membership may have been revoked during lock wait.
        self._authorize(write=True)

    def _active_catalog(self, expected: uuid.UUID) -> None:
        active_table = tenant_table_layout(self.connection).active_catalog
        current = self.connection.execute(text(f"""
            SELECT catalog_version_id
            FROM {active_table}
            WHERE organization_id=:org
            FOR UPDATE
        """), {"org": self.organization_id}).scalar_one_or_none()
        if current != expected:
            raise QuoteConflict("stale_catalog")

    def _read_revision(self, quote_id: uuid.UUID, number: int) -> StoredQuote:
        row = self.connection.execute(text("""
            SELECT quote_id, number, catalog_version_id, snapshot,
                   snapshot_sha256, is_complete, total_rub
            FROM quote_revisions
            WHERE organization_id=:org AND quote_id=:quote AND number=:number
        """), {
            "org": self.organization_id, "quote": quote_id, "number": number,
        }).mappings().one_or_none()
        if row is None:
            raise QuoteNotFound("quote_revision_not_found")
        payload = row["snapshot"]
        if not isinstance(payload, dict) or row["snapshot_sha256"] != _digest(payload):
            raise RuntimeError("Historical quote snapshot checksum mismatch.")
        content = QuoteSnapshot.model_validate(payload)
        if (
            content.total_rub != row["total_rub"]
            or content.ready_for_draft is not row["is_complete"]
        ):
            raise RuntimeError("Historical quote total or status inconsistent.")
        return StoredQuote(
            quote_id=row["quote_id"],
            revision=row["number"],
            catalog_version_id=row["catalog_version_id"],
            ready_for_draft=content.ready_for_draft,
            total_rub=content.total_rub,
            lines=content.lines,
            snapshot_sha256=row["snapshot_sha256"],
            source_request=content.source_request,
            instructions=content.instructions,
            base_total_rub=content.base_total_rub,
            estimated_total_rub=content.estimated_total_rub,
            budget_exceeded=content.budget_exceeded,
            approved=content.approved,
            document=content.document,
            approval=content.approval,
        )

    def get(self, quote_id: uuid.UUID, revision: int | None = None) -> StoredQuote:
        self._authorize(write=False)
        if revision is not None and revision < 1:
            raise QuoteNotFound("quote_revision_not_found")
        head = self.connection.scalar(text("""
            SELECT current_revision FROM quote_documents
            WHERE organization_id=:org AND id=:quote
        """), {"org": self.organization_id, "quote": quote_id})
        if head is None:
            raise QuoteNotFound("quote_not_found")
        if revision is not None and revision > head:
            raise QuoteNotFound("quote_revision_not_found")
        return self._read_revision(quote_id, revision or head)

    def _existing(self, key: str, request_hash: str) -> StoredQuote | None:
        requests_table = tenant_table_layout(self.connection).quote_idempotency
        record = self.connection.execute(text(f"""
            SELECT request_sha256, quote_id, revision_number
            FROM {requests_table}
            WHERE organization_id=:org AND key=:key
        """), {"org": self.organization_id, "key": key}).one_or_none()
        if record is None:
            return None
        if record.request_sha256 != request_hash:
            raise QuoteConflict("idempotency_conflict")
        return self._read_revision(record.quote_id, record.revision_number)

    def _append(
        self, *,
        quote_id: uuid.UUID,
        number: int,
        catalog_id: uuid.UUID,
        snapshot: QuoteSnapshot,
        key: str,
        request_hash: str,
    ) -> StoredQuote:
        payload = snapshot.model_dump(mode="json")
        digest = _digest(payload)
        self.connection.execute(_INSERT_SNAPSHOT, {
            "org": self.organization_id, "quote": quote_id,
            "number": number, "catalog": catalog_id,
            "snapshot": payload, "sha": digest,
            "ready": snapshot.ready_for_draft,
            "total": snapshot.total_rub,
        })
        requests_table = tenant_table_layout(self.connection).quote_idempotency
        self.connection.execute(text(f"""
            INSERT INTO {requests_table}(
                organization_id, key, request_sha256, quote_id, revision_number
            ) VALUES (:org, :key, :hash, :quote, :number)
        """), {
            "org": self.organization_id, "key": key, "hash": request_hash,
            "quote": quote_id, "number": number,
        })
        return self._read_revision(quote_id, number)

    def _snapshot(
        self, catalog_id: uuid.UUID, extracted: ExtractedRequest,
        selections: list[ManagerSkuSelection] | None = None,
        instructions: ConfirmedInstructions | None = None,
    ) -> QuoteSnapshot:
        # This call also revalidates A8 cookie and user identity before loading
        # products. It never returns a product from another organization.
        catalog = TenantCatalogRepository(
            self.connection, self.identity, self.organization_id,
            session_token=self.session_token, csrf_token=self.csrf_token,
        )
        return _make_snapshot(extracted, catalog.items(catalog_id), selections, instructions)

    def compare_suppliers(
        self,
        source_refs: tuple[SupplierSourceRef, ...],
        extracted: ExtractedRequest,
        instructions: ConfirmedInstructions | None,
    ) -> SupplierComparison:
        self._authorize(write=False)
        catalog = TenantCatalogRepository(
            self.connection, self.identity, self.organization_id,
            session_token=self.session_token,
        )
        sources = tuple(load_reviewed_supplier_source(catalog, ref) for ref in source_refs)
        return compare_supplier_catalogs(extracted, sources, instructions)

    def create_supplier_quote(
        self, *, active_catalog_id: uuid.UUID,
        source_refs: tuple[SupplierSourceRef, ...],
        extracted: ExtractedRequest,
        selections: tuple[SupplierOfferSelection, ...],
        instructions: ConfirmedInstructions | None,
        key: str,
    ) -> StoredQuote:
        _validate_key(key)
        self._authorize(write=True)
        if self.connection.scalar(text("""
            SELECT 1 FROM pg_constraint
            WHERE conrelid='public.tenant_quote_manager_decisions'::regclass
              AND conname='fk_quote_manager_source_tenant_version'
              AND contype='f'
        """)) != 1:
            raise RuntimeError("supplier_quote_audit_schema_unavailable")
        self._lock_tenant()
        self._active_catalog(active_catalog_id)
        comparison = self.compare_suppliers(source_refs, extracted, instructions)
        snapshot = lock_supplier_selection(extracted, comparison, selections, instructions)
        # Long comparisons must revalidate write membership before the INSERT.
        self._authorize(write=True)
        # Idempotency binds every source/version/digest, decision and policy.
        request_hash = _digest({
            "kind": "reviewed_supplier_comparison_v1",
            "active_catalog_version_id": str(active_catalog_id),
            "comparison_request_sha256": comparison.request_sha256,
            "comparison_instructions_sha256": comparison.instructions_sha256,
            "sources": [
                {
                    "supplier_id": ref.supplier_id,
                    "profile_fingerprint": ref.profile_fingerprint,
                    "catalog_version_id": str(ref.catalog_version_id),
                } for ref in source_refs
            ],
            "selections": [
                {
                    "line_number": selected.line_number,
                    "supplier_id": selected.supplier_id,
                    "catalog_version_id": str(selected.catalog_version_id),
                    "sku": selected.sku,
                } for selected in sorted(selections, key=lambda item: item.line_number)
            ],
        })
        existing = self._existing(key, request_hash)
        if existing is not None:
            return existing
        quote_id = uuid.uuid4()
        self.connection.execute(text("""
            INSERT INTO quote_documents(id,organization_id,current_revision)
            VALUES (:id,:org,1)
        """), {"id": quote_id, "org": self.organization_id})
        return self._append(
            quote_id=quote_id, number=1, catalog_id=active_catalog_id,
            snapshot=snapshot, key=key, request_hash=request_hash,
        )

    def create(
        self, catalog_id: uuid.UUID, extracted: ExtractedRequest, key: str,
        selections: list[ManagerSkuSelection] | None = None,
        instructions: ConfirmedInstructions | None = None,
    ) -> StoredQuote:
        _validate_key(key)
        self._authorize(write=True)
        self._lock_tenant()
        self._active_catalog(catalog_id)
        request_hash = _hash_request(catalog_id, extracted, None, None, selections, instructions)
        existing = self._existing(key, request_hash)
        if existing is not None:
            return existing
        snapshot = self._snapshot(catalog_id, extracted, selections, instructions)
        quote_id = uuid.uuid4()
        self.connection.execute(text("""
            INSERT INTO quote_documents(
                id, organization_id, current_revision
            ) VALUES (:id, :org, 1)
        """), {"id": quote_id, "org": self.organization_id})
        return self._append(
            quote_id=quote_id, number=1, catalog_id=catalog_id,
            snapshot=snapshot, key=key, request_hash=request_hash,
        )

    def document_alternatives(self, quote: StoredQuote) -> tuple[DocumentAlternative, ...]:
        self._authorize(write=True)
        catalog = TenantCatalogRepository(
            self.connection, self.identity, self.organization_id,
            session_token=self.session_token, csrf_token=self.csrf_token,
        )
        return available_alternatives(quote, catalog.items(quote.catalog_version_id))

    def resolve_document_alternatives(
        self, quote: StoredQuote, selections: tuple[AlternativeSelection, ...],
    ) -> tuple[DocumentAlternative, ...]:
        self._authorize(write=True)
        catalog = TenantCatalogRepository(
            self.connection, self.identity, self.organization_id,
            session_token=self.session_token, csrf_token=self.csrf_token,
        )
        return resolve_alternatives(quote, catalog.items(quote.catalog_version_id), selections)

    def validate_document_context(
        self, current: StoredQuote, document: CommercialDocument, *, lock_profile: bool = False,
    ) -> None:
        if document.alternatives:
            actual = self.resolve_document_alternatives(current, tuple(item.selection for item in document.alternatives))
            if actual != document.alternatives:
                raise ValueError("alternative_snapshot_mismatch")
        if "company_profile" in document.model_fields_set:
            actual_profile = CompanyProfileStore(
                self.connection, self.organization_id, self.session_token, self.csrf_token,
            ).get(for_update=lock_profile)
            if actual_profile != document.company_profile:
                raise QuoteConflict("stale_company_profile")

    def approve_document(
        self, quote_id: uuid.UUID, expected_revision: int,
        source_snapshot_sha256: str, document: CommercialDocument, key: str,
    ) -> StoredQuote:
        """Append an approved document without re-reading or changing locked prices."""
        _validate_key(key)
        self._authorize(write=True)
        self._lock_tenant()
        request_hash = _digest({
            "operation": "approve_document", "quote_id": str(quote_id),
            "expected_revision": expected_revision,
            "source_snapshot_sha256": source_snapshot_sha256,
            "document": document.model_dump(mode="json"),
            **({"company_profile_absent": True} if document.company_profile is None
               and "company_profile" in document.model_fields_set else {}),
        })
        existing = self._existing(key, request_hash)
        if existing is not None:
            return existing
        head = self.connection.scalar(text("""
            SELECT current_revision FROM quote_documents
            WHERE organization_id=:org AND id=:quote FOR UPDATE
        """), {"org": self.organization_id, "quote": quote_id})
        if head is None:
            raise QuoteNotFound("quote_not_found")
        if head != expected_revision:
            raise QuoteConflict("stale_revision")
        current = self._read_revision(quote_id, head)
        if current.snapshot_sha256 != source_snapshot_sha256:
            raise QuoteConflict("stale_snapshot")
        if current.approved:
            raise QuoteConflict("quote_already_approved")
        if not current.ready_for_draft or current.total_rub is None:
            raise QuoteConflict("quote_not_ready_for_approval")
        self.validate_document_context(current, document, lock_profile=True)
        snapshot = QuoteSnapshot(
            ready_for_draft=True, total_rub=current.total_rub, lines=current.lines,
            source_request=current.source_request, instructions=current.instructions,
            base_total_rub=current.base_total_rub,
            estimated_total_rub=current.estimated_total_rub,
            budget_exceeded=current.budget_exceeded,
            approved=True, document=document,
            approval=DocumentApproval(
                user_id=self.identity.user_id, approved_at=datetime.now(UTC),
                source_revision=head, source_snapshot_sha256=current.snapshot_sha256,
            ),
        )
        updated = self.connection.execute(text("""
            UPDATE quote_documents SET current_revision=:number
            WHERE organization_id=:org AND id=:quote AND current_revision=:expected
        """), {
            "org": self.organization_id, "quote": quote_id,
            "number": head + 1, "expected": expected_revision,
        })
        if updated.rowcount != 1:
            raise QuoteConflict("stale_revision")
        return self._append(
            quote_id=quote_id, number=head + 1, catalog_id=current.catalog_version_id,
            snapshot=snapshot, key=key, request_hash=request_hash,
        )

    def revise(
        self, quote_id: uuid.UUID, expected_revision: int,
        catalog_id: uuid.UUID, extracted: ExtractedRequest, key: str,
        selections: list[ManagerSkuSelection] | None = None,
        instructions: ConfirmedInstructions | None = None,
    ) -> StoredQuote:
        _validate_key(key)
        if expected_revision < 1:
            raise QuoteConflict("stale_revision")
        self._authorize(write=True)
        self._lock_tenant()
        self._active_catalog(catalog_id)
        request_hash = _hash_request(
            catalog_id, extracted, quote_id, expected_revision, selections, instructions,
        )
        existing = self._existing(key, request_hash)
        if existing is not None:
            return existing
        head = self.connection.scalar(text("""
            SELECT current_revision FROM quote_documents
            WHERE organization_id=:org AND id=:quote
            FOR UPDATE
        """), {"org": self.organization_id, "quote": quote_id})
        if head is None:
            raise QuoteNotFound("quote_not_found")
        if head != expected_revision:
            raise QuoteConflict("stale_revision")
        snapshot = self._snapshot(catalog_id, extracted, selections, instructions)
        next_number = head + 1
        updated = self.connection.execute(text("""
            UPDATE quote_documents SET current_revision=:number
            WHERE organization_id=:org AND id=:quote
              AND current_revision=:expected
        """), {
            "org": self.organization_id, "quote": quote_id,
            "number": next_number, "expected": expected_revision,
        })
        if updated.rowcount != 1:
            raise QuoteConflict("stale_revision")
        return self._append(
            quote_id=quote_id, number=next_number, catalog_id=catalog_id,
            snapshot=snapshot, key=key, request_hash=request_hash,
        )
