"""A35 authenticated reviewer consensus and separately blocked source provenance."""
from __future__ import annotations

import hashlib
import json
import uuid
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from test_tenant_candidate_http import ORG, make_bundle

from kpdesk.api import tenant_candidate_review_routes as routes
from kpdesk.api.tenant_session_routes import get_unreleased_auth_repository
from kpdesk.catalog.supplier_precision_storage import TenantSupplierPrecisionReviewStore
from kpdesk.main import create_app
from kpdesk.matching.tenant_candidate_consensus import calculate_tenant_consensus
from kpdesk.matching.tenant_candidate_evidence import verify_candidate_archive
from kpdesk.matching.tenant_source_restrictions import (
    ReviewFinding,
    TenantSourceRestrictionStore,
)

ONE = uuid.UUID("96000000-0000-4000-8000-000000000001")
TWO = uuid.UUID("96000000-0000-4000-8000-000000000002")


class HistoryStub:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def history(self, queue_sha256: str, *, limit: int, offset: int) -> list[dict[str, Any]]:
        assert queue_sha256 == "a" * 64
        return self.rows[offset:offset + limit]


def record(reviewer: uuid.UUID, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "request_id": "request-a", "task_sha256": "b" * 64,
        "source_sha256": "c" * 64, "source_batch_id": "batch-a",
        "partition": "train", "reviewer_user_id": reviewer,
        "candidate_skus": ["SKU-1", "SKU-2"],
        "accepted_skus": ["SKU-1"],
        "rejected_skus": ["SKU-2"],
        "unresolved_skus": [],
        "no_candidate_match": False,
    }
    values.update(overrides)
    return values


def test_consensus_needs_two_distinct_authenticated_users_and_no_unresolved() -> None:
    stub = HistoryStub([record(ONE)])
    first = calculate_tenant_consensus(stub, "a" * 64)
    assert first["agreed"] == []
    assert first["incomplete"][0]["reviewer_count"] == 1
    stub.rows.append(record(TWO))
    agreed = calculate_tenant_consensus(stub, "a" * 64)
    assert len(agreed["agreed"]) == 1
    assert agreed["agreed"][0]["reviewer_user_ids"] == sorted([str(ONE), str(TWO)])
    assert agreed["independent_gold"] is False
    assert agreed["training_approved"] is False

    stub.rows[1]["accepted_skus"] = []
    stub.rows[1]["rejected_skus"] = ["SKU-1", "SKU-2"]
    stub.rows[1]["no_candidate_match"] = True
    conflicts = calculate_tenant_consensus(stub, "a" * 64)
    assert conflicts["agreed"] == []
    assert len(conflicts["conflicts"]) == 1

    stub.rows[1] = record(TWO, accepted_skus=[], rejected_skus=["SKU-2"],
                          unresolved_skus=["SKU-1"])
    stub.rows[0] = record(ONE, accepted_skus=[], rejected_skus=["SKU-2"],
                          unresolved_skus=["SKU-1"])
    uncertain = calculate_tenant_consensus(stub, "a" * 64)
    assert uncertain["agreed"] == []
    assert len(uncertain["incomplete"]) == 1
    stub.rows[1] = record(ONE)
    with pytest.raises(ValueError, match="identity"):
        calculate_tenant_consensus(stub, "a" * 64)


def test_consensus_fails_closed_on_incomplete_history_scan() -> None:
    rows = [record(uuid.uuid4()) for _ in range(101)]
    with pytest.raises(ValueError, match="bounded"):
        calculate_tenant_consensus(HistoryStub(rows), "a" * 64, max_reviews=100)


def test_restriction_store_never_claims_rights_approval_and_verifies_original(
    tmp_path: Path,
) -> None:
    payload, _, _ = make_bundle(tmp_path)
    original = verify_candidate_archive(payload)
    unknown = "f" * 64
    scope = SimpleNamespace()
    store = TenantSourceRestrictionStore(cast(TenantSupplierPrecisionReviewStore, scope))
    with pytest.raises(ValueError, match="does not belong"):
        store.attest(
            original, source_sha256=unknown, review_kind="rights",
            finding="restricted", evidence_sha256=None,
            rationale="Source has no independent licensed provenance documentation.",
        )
    source = next(iter(original.source_sha256s))
    with pytest.raises(ValueError, match="Only blocking"):
        store.attest(
            original, source_sha256=source, review_kind="rights",
            finding=cast(ReviewFinding, "approved"), evidence_sha256=None,
            rationale="This source cannot assert a verified commercial license.",
        )


def test_restriction_http_validates_source_and_sessions_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload, original_decision, _ = make_bundle(tmp_path)
    original = verify_candidate_archive(payload)
    reviewed_user = ONE
    scope = SimpleNamespace(identity=SimpleNamespace(user_id=reviewed_user))
    recorded: list[str] = []

    class Stub:
        def __init__(self, candidate_scope: object) -> None:
            assert candidate_scope is scope

        def _authorize(self, *, write: bool) -> None:
            assert write

        def attest(self, evidence: object, **kwargs: object) -> bool:
            recorded.append(str(kwargs["finding"]))
            return True

    monkeypatch.setattr(routes, "_store", lambda *_args, **_kwargs: scope)
    monkeypatch.setattr(routes, "TenantSourceRestrictionStore", Stub)
    app = create_app(testing=True, tenant_sessions=True)
    app.dependency_overrides[get_unreleased_auth_repository] = lambda: object()
    source = next(iter(original.source_sha256s))
    decision = {
        "archive_sha256": hashlib.sha256(payload).hexdigest(),
        "queue_sha256": original_decision["queue_sha256"],
        "source_sha256": source,
        "review_kind": "privacy", "finding": "requires_evidence",
        "evidence_sha256": None,
        "rationale": "Original customer request needs redaction and independent privacy evidence.",
        "personally_reviewed": True,
    }
    headers = {"Origin": "https://localhost:5173", "X-CSRF-Token": "test"}
    with TestClient(app, base_url="https://testserver") as client:
        def submit(body: dict[str, Any]) -> int:
            response: Response = client.post(
                f"/api/session/organizations/{ORG}/candidate-reviews/source-restrictions",
                files={"evidence": ("original.zip", payload)},
                headers=headers,
                data={"review_json": json.dumps(body)},
            )
            return response.status_code

        wrong_source = deepcopy(decision)
        wrong_source["source_sha256"] = "0" * 64
        assert submit(wrong_source) == 422
        elevated = deepcopy(decision)
        elevated["finding"] = "approved"
        assert submit(elevated) == 422
        missing = deepcopy(decision)
        missing.pop("personally_reviewed")
        assert submit(missing) == 422
        assert recorded == []
        assert submit(decision) == 201
        assert recorded == ["requires_evidence"]




def test_governance_routes_require_real_session() -> None:
    with TestClient(
        create_app(testing=True, tenant_sessions=True),
        base_url="https://testserver",
    ) as anonymous:
        base = f"/api/session/organizations/{ORG}/candidate-reviews"
        assert anonymous.get(
            base + "/consensus", params={"queue_sha256": "a" * 64},
        ).status_code == 401
        assert anonymous.get(
            base + "/source-restrictions", params={"queue_sha256": "a" * 64},
        ).status_code == 401
