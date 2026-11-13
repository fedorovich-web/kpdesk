"""Owner-only independent candidate conflict arbitration (not ML gold)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from kpdesk.catalog.supplier_precision_storage import TenantSupplierPrecisionReviewStore
from kpdesk.db.tenant_access import TenantAccessDenied
from kpdesk.matching.request_annotations import _sha
from kpdesk.matching.tenant_candidate_evidence import VerifiedCandidateEvidence

type AdjudicationFinding = Literal["resolve_proposals", "needs_evidence", "reject_case"]
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_ATTEST = text("""
 SELECT public.kpdesk_candidate_adjudicate(
   :org,:queue,:task,:source,:request,:candidates,:finding,
   :accepted,:rejected,:no_match,:rationale
 )
""").bindparams(*(
    bindparam(name, type_=JSONB) for name in ("candidates", "accepted", "rejected")
))
_GET = text("""
 SELECT task_sha256,source_sha256,request_id,candidate_skus,finding,
        accepted_skus,rejected_skus,no_candidate_match,rationale,
        adjudicator_user_id,original_reviewer_count,recorded_at
 FROM public.kpdesk_candidate_adjudications_get(:org,:queue,:limit,:offset)
""")


@dataclass(frozen=True, slots=True)
class TenantCandidateAdjudicationStore:
    scope: TenantSupplierPrecisionReviewStore

    def _authorize(self, *, write: bool) -> None:
        self.scope._authorize(write=write)
        if self.scope.connection.scalar(text("""
            SELECT to_regprocedure(
              'public.kpdesk_candidate_adjudicate(uuid,text,text,text,text,jsonb,text,jsonb,jsonb,boolean,text)'
            ) IS NOT NULL AND to_regprocedure(
              'public.kpdesk_candidate_adjudications_get(uuid,text,integer,integer)'
            ) IS NOT NULL
        """)) is not True:
            raise RuntimeError("Adjudication schema 0027 unavailable.")

    def attest(
        self, original: VerifiedCandidateEvidence, *,
        request_id: str, task_sha256: str, finding: AdjudicationFinding,
        accepted_skus: list[str], rejected_skus: list[str],
        no_candidate_match: bool, rationale: str,
    ) -> bool:
        if not isinstance(original, VerifiedCandidateEvidence):
            raise TypeError("Verified original request evidence is required.")
        task = original.tasks.get(request_id)
        if task is None or task_sha256 != _sha(task):
            raise ValueError("Conflict source task is not in verified candidate queue.")
        if finding not in ("resolve_proposals", "needs_evidence", "reject_case"):
            raise ValueError("Arbitration finding is invalid.")
        if not isinstance(rationale, str) or not 32 <= len(rationale.strip()) <= 1000:
            raise ValueError("Arbitration explanation invalid.")
        if any(ord(ch) < 32 for ch in rationale):
            raise ValueError("Arbitration rationale contains control characters.")
        if type(no_candidate_match) is not bool:
            raise TypeError("Explicit scoped no-match flag required.")
        if not isinstance(accepted_skus, list) or not isinstance(rejected_skus, list):
            raise TypeError("Reviewed candidate groups must be lists.")
        shown = task["candidate_skus"]
        if finding == "resolve_proposals":
            all_skus = [*accepted_skus, *rejected_skus]
            if (
                len(all_skus) != len(shown)
                or len(set(all_skus)) != len(all_skus)
                or set(all_skus) != set(shown)
                or (no_candidate_match and (accepted_skus or rejected_skus != shown))
                or (not no_candidate_match and not accepted_skus)
            ):
                raise ValueError("All displayed candidates need an exact final judgment.")
        elif accepted_skus or rejected_skus or no_candidate_match:
            raise ValueError("Deferred or rejected dispute cannot include approved labels.")
        self._authorize(write=True)
        approved = self.scope.connection.scalar(_ATTEST, {
            "org": self.scope.organization_id,
            "queue": original.queue_sha256, "task": task_sha256,
            "source": task["source_sha256"], "request": request_id,
            "candidates": shown, "finding": finding,
            "accepted": accepted_skus, "rejected": rejected_skus,
            "no_match": no_candidate_match, "rationale": rationale,
        })
        if approved is not True:
            raise TenantAccessDenied(
                "Conflict requires 2 independent reviewers and an unrelated owner."
            )
        return True

    def history(self, queue_sha256: str, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        if not isinstance(queue_sha256, str) or _SHA.fullmatch(queue_sha256) is None:
            raise ValueError("Invalid adjudication queue identifier.")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Adjudication page size invalid.")
        if type(offset) is not int or not 0 <= offset <= 5000:
            raise ValueError("Adjudication page offset invalid.")
        self._authorize(write=False)
        return [dict(row) for row in self.scope.connection.execute(_GET, {
            "org": self.scope.organization_id, "queue": queue_sha256,
            "limit": limit, "offset": offset,
        }).mappings().all()]
