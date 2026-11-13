"""Authenticated, immutable supplier price-unit attestations.

Only an operator's source-bound statement; never canonical authorization,
commercial price verification or an ML relevance label.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text

from kpdesk.catalog.supplier_canonical_preview import CanonicalUnit, source_row_sha256
from kpdesk.catalog.supplier_precision_storage import TenantSupplierPrecisionReviewStore
from kpdesk.catalog.supplier_staging import StagedSupplierRow, SupplierStaging
from kpdesk.db.tenant_access import TenantAccessDenied

type UnitFinding = Literal[
    "confirm_price_unit", "needs_supplier_confirmation", "reject_source_unit",
]
_ATTEST = text("""
    SELECT public.kpdesk_supplier_unit_attest(
        :org,:supplier,:source_sha,:line,:row_sha,:source_unit,:source_pack,
        :finding,:approved_unit,:approved_pack,:rationale
    )
""")
_GET = text("""
    SELECT source_line,source_row_sha256,source_sale_unit,source_pack_size,
           finding,approved_sale_unit,approved_pack_size,rationale,
           reviewer_user_id,reviewed_at
    FROM public.kpdesk_supplier_unit_get(
        :org,:supplier,:source_sha,:limit,:offset
    )
""")


@dataclass(frozen=True, slots=True)
class TenantSupplierUnitReviewStore:
    """Reuse the established precision-store session, CSRF and role binding."""

    scope: TenantSupplierPrecisionReviewStore

    def _authorize(self, *, write: bool) -> None:
        self.scope._authorize(write=write)
        if self.scope.connection.scalar(text("""
            SELECT to_regprocedure(
              'public.kpdesk_supplier_unit_attest(uuid,text,text,integer,text,text,integer,text,text,integer,text)'
            ) IS NOT NULL
            AND to_regprocedure(
              'public.kpdesk_supplier_unit_get(uuid,text,text,integer,integer)'
            ) IS NOT NULL
        """)) is not True:
            raise RuntimeError("Supplier unit audit migration 0024 is unavailable.")

    def attest(
        self, staging: SupplierStaging, supplier_id: str, row: StagedSupplierRow,
        *, finding: UnitFinding, approved_sale_unit: CanonicalUnit | None,
        approved_pack_size: int | None, rationale: str,
    ) -> bool:
        self.scope._selector(supplier_id, staging.sha256)
        if (
            not isinstance(staging, SupplierStaging)
            or not isinstance(row, StagedSupplierRow)
            or not any(original == row and original.line == row.line
                       for original in staging.rows)
            or not isinstance(rationale, str)
            or not 32 <= len(rationale.strip()) <= 500
            or any(ord(ch) < 32 for ch in rationale)
            or finding not in (
                "confirm_price_unit", "needs_supplier_confirmation", "reject_source_unit",
            )
        ):
            raise ValueError("Invalid source-bound supplier unit review.")
        if finding == "confirm_price_unit":
            if (
                approved_sale_unit not in ("штука", "пачка", "коробка")
                or type(approved_pack_size) is not int
                or not 1 <= approved_pack_size <= 100000
                or "unconfirmed_or_non_rub_currency" in row.issues
                or (row.price_rub is None and row.source_high_precision_price is None)
                or row.formula_columns
                or "xls_formula_and_macro_provenance_unverified" in row.issues
            ):
                raise ValueError("Cannot confirm a unit on unverified supplier provenance.")
        elif approved_sale_unit is not None or approved_pack_size is not None:
            raise ValueError("Unresolved unit review must not contain canonical values.")
        self._authorize(write=True)
        result = self.scope.connection.scalar(_ATTEST, {
            "org": self.scope.organization_id,
            "supplier": supplier_id, "source_sha": staging.sha256,
            "line": row.line, "row_sha": source_row_sha256(row),
            "source_unit": row.source_sale_unit, "source_pack": row.source_pack_size,
            "finding": finding, "approved_unit": approved_sale_unit,
            "approved_pack": approved_pack_size, "rationale": rationale,
        })
        if result is not True:
            raise TenantAccessDenied("PostgreSQL rejected supplier unit attestation.")
        return True

    def list_reviews(
        self, supplier_id: str, source_sha256: str,
        *, limit: int = 50, offset: int = 0,
    ) -> list[dict[str, object]]:
        self.scope._selector(supplier_id, source_sha256)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Unit history page limit invalid.")
        if type(offset) is not int or not 0 <= offset <= 5000:
            raise ValueError("Unit history page offset invalid.")
        self._authorize(write=False)
        records = self.scope.connection.execute(_GET, {
            "org": self.scope.organization_id, "supplier": supplier_id,
            "source_sha": source_sha256, "limit": limit, "offset": offset,
        }).mappings().all()
        return [dict(record) for record in records]
