"""Authenticated, source-bound PostgreSQL precision review persistence.

Unlike offline JSON reviewer IDs, this repository binds an actual verified
tenant session, user identity and CSRF before invoking append-only SQL.
No catalog publishing, no model training and no transaction commits here.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from sqlalchemy import Connection, text

from kpdesk.catalog.supplier_canonical_preview import (
    ReviewedPrecisionPrice,
    checked_precision_rounding,
    source_row_sha256,
)
from kpdesk.catalog.supplier_staging import (
    StagedSupplierRow,
    SupplierStaging,
    _source_high_precision_amount,
)
from kpdesk.db.authenticated_tenant import bind_authenticated_tenant
from kpdesk.db.tenant_access import AuthenticatedUser, TenantAccessDenied

type PrecisionFinding = Literal[
    "approve_rounding", "needs_supplier_confirmation", "reject_source_price",
]
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_SUPPLIER = re.compile(r"[A-Za-z0-9_.-]{1,120}\Z")
_ATTEST = text("""
    SELECT public.kpdesk_supplier_precision_attest(
        :org,:supplier,:source_sha,:line,:row_sha,:price_raw,:exact,
        :finding,:rounding_mode,:rounded_price,:rationale
    )
""")
_GET = text("""
    SELECT source_line,source_row_sha256,source_price_raw,
           finding,rounding_mode,rounded_price_rub,rationale,
           reviewer_user_id,reviewed_at
    FROM public.kpdesk_supplier_precision_get(:org,:supplier,:source_sha)
""")


@dataclass(frozen=True, slots=True)
class TenantSupplierPrecisionReviewStore:
    connection: Connection
    identity: AuthenticatedUser
    organization_id: uuid.UUID
    session_token: str
    csrf_token: str

    def _authorize(self, *, write: bool) -> None:
        if not self.connection.in_transaction():
            raise RuntimeError("Supplier precision audit requires an active transaction.")
        scope = bind_authenticated_tenant(
            self.connection,
            session_token=self.session_token,
            organization_id=self.organization_id,
            operation="write" if write else "read",
            csrf=self.csrf_token if write else None,
        )
        if scope.identity.user_id != self.identity.user_id:
            self.connection.execute(text("""
                SELECT set_config('kpdesk.session_sha256', '', true),
                       set_config('kpdesk.organization_id', '', true)
            """))
            raise TenantAccessDenied("Supplier price review session identity mismatch.")
        if self.connection.scalar(text("""
            SELECT to_regprocedure(
                'public.kpdesk_supplier_precision_attest(uuid,text,text,integer,text,text,numeric,text,text,numeric,text)'
            ) IS NOT NULL
        """)) is not True:
            raise RuntimeError("Supplier precision audit migration 0021 is unavailable.")

    @staticmethod
    def _selector(supplier_id: str, source_sha256: str) -> None:
        if (
            not isinstance(supplier_id, str)
            or _SUPPLIER.fullmatch(supplier_id) is None
            or not isinstance(source_sha256, str)
            or _SHA.fullmatch(source_sha256) is None
        ):
            raise ValueError("Supplier precision review identity is invalid.")

    def attest(
        self,
        staging: SupplierStaging,
        supplier_id: str,
        row: StagedSupplierRow,
        *,
        finding: PrecisionFinding,
        precision_review: ReviewedPrecisionPrice | None,
        rationale: str,
    ) -> bool:
        """Persist only the authenticated user's own original-row finding."""
        if not isinstance(staging, SupplierStaging):
            raise TypeError("Verified supplier staging is required.")
        self._selector(supplier_id, staging.sha256)
        if (
            not isinstance(row, StagedSupplierRow)
            or not any(row.line == source.line and row == source for source in staging.rows)
            or not isinstance(finding, str)
            or finding not in (
                "approve_rounding", "needs_supplier_confirmation", "reject_source_price",
            )
            or not isinstance(rationale, str)
            or not 32 <= len(rationale.strip()) <= 500
            or any(ord(ch) < 32 for ch in rationale)
        ):
            raise ValueError("Supplier precision review has invalid row or rationale.")
        source_raw = row.source_high_precision_price
        exact = _source_high_precision_amount(source_raw)
        if source_raw is None or exact is None:
            raise ValueError("Only real over-precision source prices can be reviewed.")
        if finding == "approve_rounding":
            if not isinstance(precision_review, ReviewedPrecisionPrice):
                raise ValueError("Approved precision review requires explicit rounding.")
            if precision_review.explanation != rationale:
                raise ValueError("Precision review explanation must match audit rationale.")
            rounded: Decimal | None = checked_precision_rounding(row, precision_review)
            mode = precision_review.rounding_mode
        else:
            if precision_review is not None:
                raise ValueError("Unresolved precision review cannot contain rounded price.")
            rounded = None
            mode = None
        self._authorize(write=True)
        approved = self.connection.scalar(_ATTEST, {
            "org": self.organization_id,
            "supplier": supplier_id,
            "source_sha": staging.sha256,
            "line": row.line,
            "row_sha": source_row_sha256(row),
            "price_raw": source_raw,
            "exact": exact,
            "finding": finding,
            "rounding_mode": mode,
            "rounded_price": rounded,
            "rationale": rationale,
        })
        if approved is not True:
            raise TenantAccessDenied("PostgreSQL rejected supplier precision attestation.")
        return True

    def list_reviews(
        self, supplier_id: str, source_sha256: str,
    ) -> list[dict[str, object]]:
        """Read only reviews from one source and the authenticated organization."""
        self._selector(supplier_id, source_sha256)
        self._authorize(write=False)
        records = self.connection.execute(_GET, {
            "org": self.organization_id,
            "supplier": supplier_id,
            "source_sha": source_sha256,
        }).mappings().all()
        return [dict(record) for record in records]
