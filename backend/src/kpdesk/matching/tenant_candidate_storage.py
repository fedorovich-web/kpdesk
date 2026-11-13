"""SQLAlchemy persistence for session-authenticated human candidate judgments."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from kpdesk.catalog.supplier_precision_storage import TenantSupplierPrecisionReviewStore
from kpdesk.db.tenant_access import TenantAccessDenied
from kpdesk.matching.request_annotations import CandidateDecision
from kpdesk.matching.tenant_candidate_evidence import VerifiedCandidateEvidence

_ATTEST = text("""
    SELECT public.kpdesk_candidate_review_attest(
      :org,:queue,:task,:intake,:catalog,:split,:request,:source,:batch,
      :partition,:candidates,:positive,:negative,:unresolved,:no_match,:rationale
    )
""").bindparams(*(
    bindparam(name, type_=JSONB)
    for name in ("candidates", "positive", "negative", "unresolved")
))
_HISTORY = text("""
    SELECT task_sha256,request_id,source_sha256,source_batch_id,
      partition,candidate_skus,accepted_skus,rejected_skus,unresolved_skus,
      no_candidate_match,rationale,reviewer_user_id,recorded_at
    FROM public.kpdesk_candidate_review_get(:org,:queue,:limit,:offset)
""")


@dataclass(frozen=True, slots=True)
class TenantCandidateReviewStore:
    scope: TenantSupplierPrecisionReviewStore

    def _authorize(self, *, write: bool) -> None:
        self.scope._authorize(write=write)
        signature = (
            "public.kpdesk_candidate_review_attest("
            "uuid,text,text,text,text,text,text,text,text,text,"
            "jsonb,jsonb,jsonb,jsonb,boolean,text)"
        )
        if self.scope.connection.scalar(text("""
            SELECT to_regprocedure(:signature) IS NOT NULL
            AND to_regprocedure(
              'public.kpdesk_candidate_review_get(uuid,text,integer,integer)'
            ) IS NOT NULL
        """), {"signature": signature}) is not True:
            raise RuntimeError("Candidate review schema 0025 not installed.")

    def attest(
        self, evidence: VerifiedCandidateEvidence, decision: CandidateDecision,
    ) -> bool:
        if not isinstance(evidence, VerifiedCandidateEvidence):
            raise TypeError("Verified request original evidence required.")
        if not isinstance(decision, CandidateDecision):
            raise TypeError("Validated human source decision required.")
        task = evidence.tasks.get(decision.request_id)
        if task is None:
            raise ValueError("Request is absent from verified candidate queue.")
        from kpdesk.matching.request_annotations import _sha
        if _sha(task) != decision.task_sha256:
            raise ValueError("Review task digest changed.")
        self._authorize(write=True)
        # Reviewer UUID is read from a verified server-side session, never
        # from an uploaded JSON reviewer name.
        if decision.reviewed_by != str(self.scope.identity.user_id):
            raise TenantAccessDenied("Review identity not bound to current session.")
        result = self.scope.connection.scalar(_ATTEST, {
            "org": self.scope.organization_id,
            "queue": evidence.queue_sha256,
            "task": decision.task_sha256,
            "intake": evidence.intake_sha256,
            "catalog": evidence.catalog_sha256,
            "split": evidence.split_sha256,
            "request": decision.request_id,
            "source": task["source_sha256"],
            "batch": task["source_batch_id"],
            "partition": task["partition"],
            "candidates": task["candidate_skus"],
            "positive": list(decision.accepted_skus),
            "negative": list(decision.rejected_skus),
            "unresolved": list(decision.unresolved_skus),
            "no_match": decision.no_candidate_match,
            "rationale": decision.rationale,
        })
        if result is not True:
            raise TenantAccessDenied("PostgreSQL denied or detected conflicting review.")
        return True

    def history(self, queue_sha256: str, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        if not isinstance(queue_sha256, str) or len(queue_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in queue_sha256
        ):
            raise ValueError("Invalid queue digest.")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid history page size.")
        if type(offset) is not int or not 0 <= offset <= 5000:
            raise ValueError("Invalid history offset.")
        self._authorize(write=False)
        return [dict(row) for row in self.scope.connection.execute(_HISTORY, {
            "org": self.scope.organization_id,
            "queue": queue_sha256, "limit": limit, "offset": offset,
        }).mappings().all()]
