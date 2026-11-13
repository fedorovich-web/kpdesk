"""Authenticated immutable source rights/privacy restriction audit (never grants)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import text

from kpdesk.catalog.supplier_precision_storage import TenantSupplierPrecisionReviewStore
from kpdesk.db.tenant_access import TenantAccessDenied
from kpdesk.matching.tenant_candidate_evidence import VerifiedCandidateEvidence

type ReviewKind = Literal["rights", "privacy"]
type ReviewFinding = Literal["requires_evidence", "restricted", "rejected"]
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_ATTEST = text("""
 SELECT public.kpdesk_request_restriction_attest(
   :org,:queue,:source,:kind,:finding,:evidence,:rationale
 )
""")
_GET = text("""
 SELECT source_sha256,review_kind,finding,evidence_sha256,rationale,
        reviewer_user_id,recorded_at
 FROM public.kpdesk_request_restriction_get(:org,:queue,:limit,:offset)
""")


@dataclass(frozen=True, slots=True)
class TenantSourceRestrictionStore:
    scope: TenantSupplierPrecisionReviewStore

    def _authorize(self, *, write: bool) -> None:
        self.scope._authorize(write=write)
        if self.scope.connection.scalar(text("""
           SELECT to_regprocedure(
             'public.kpdesk_request_restriction_attest(uuid,text,text,text,text,text,text)'
           ) IS NOT NULL AND to_regprocedure(
             'public.kpdesk_request_restriction_get(uuid,text,integer,integer)'
           ) IS NOT NULL
        """)) is not True:
            raise RuntimeError("Source restriction audit schema 0026 unavailable.")

    def attest(
        self, original: VerifiedCandidateEvidence, *,
        source_sha256: str, review_kind: ReviewKind,
        finding: ReviewFinding, evidence_sha256: str | None,
        rationale: str,
    ) -> bool:
        if not isinstance(original, VerifiedCandidateEvidence):
            raise TypeError("Original verified source archive required.")
        if source_sha256 not in original.source_sha256s:
            raise ValueError("Source does not belong to verified intake.")
        if review_kind not in ("rights", "privacy") or finding not in (
            "requires_evidence", "restricted", "rejected",
        ):
            raise ValueError("Only blocking source findings are supported.")
        if evidence_sha256 is not None and (
            not isinstance(evidence_sha256, str) or _DIGEST.fullmatch(evidence_sha256) is None
        ):
            raise ValueError("Evidence fingerprint invalid.")
        if not isinstance(rationale, str) or not 32 <= len(rationale.strip()) <= 1000:
            raise ValueError("Evidence review rationale invalid.")
        if any(ord(char) < 32 for char in rationale):
            raise ValueError("Review rationale contains control characters.")
        self._authorize(write=True)
        recorded = self.scope.connection.scalar(_ATTEST, {
            "org": self.scope.organization_id,
            "queue": original.queue_sha256,
            "source": source_sha256, "kind": review_kind,
            "finding": finding, "evidence": evidence_sha256,
            "rationale": rationale,
        })
        if recorded is not True:
            raise TenantAccessDenied("Source restriction denied or conflicts.")
        return True

    def history(self, queue_sha256: str, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        if not isinstance(queue_sha256, str) or _DIGEST.fullmatch(queue_sha256) is None:
            raise ValueError("Queue SHA256 invalid.")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("History page size invalid.")
        if type(offset) is not int or not 0 <= offset <= 5000:
            raise ValueError("History offset invalid.")
        self._authorize(write=False)
        return [dict(row) for row in self.scope.connection.execute(_GET, {
            "org": self.scope.organization_id,
            "queue": queue_sha256, "limit": limit, "offset": offset,
        }).mappings().all()]
