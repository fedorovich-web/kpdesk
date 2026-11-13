"""Session-only supplier precision audit API, never legacy owner-token auth."""
from __future__ import annotations

import hashlib
import json
import uuid

from fastapi.testclient import TestClient
from httpx import Response
from pytest import MonkeyPatch

from kpdesk.main import create_app

ORG = uuid.UUID("9f000000-0000-4000-8000-000000000001")
PATH = f"/api/session/organizations/{ORG}/suppliers/vendor-1/precision-reviews"
CSV = b"sku;name;price;currency;unit;pack\nA1;Clips;10.125;RUB;pcs;1\n"


def _mapping() -> dict[str, int]:
    return {
        "sku": 0, "name": 1, "price": 2, "currency": 3,
        "sale_unit": 4, "pieces_per_sale_unit": 5, "header_row": 1,
    }


def _payload() -> dict[str, object]:
    return {
        "source_sha256": hashlib.sha256(CSV).hexdigest(),
        "mapping": _mapping(),
        "line": 2,
        "source_row_sha256": "a" * 64,
        "finding": "approve_rounding",
        "rounding_mode": "half_up",
        "rounded_price_rub": "10.13",
        "rationale": (
            "An actual supplier price policy must be separately verified "
            "before any rounding is attested."
        ),
    }


def test_precision_audit_routes_never_exist_on_legacy_owner_app() -> None:
    app = create_app(testing=True)
    with TestClient(app) as client:
        assert client.get(PATH, params={"source_sha256": "b" * 64}).status_code == 404
        response = client.post(
            PATH, headers={"Origin": "http://localhost:5173"},
            files={"file": ("vendor.csv", CSV, "text/csv")},
            data={"review_json": json.dumps(_payload())},
        )
        assert response.status_code == 404


def test_precision_audit_requires_real_session_and_cannot_use_owner_secret() -> None:
    app = create_app(testing=True, tenant_sessions=True)
    with TestClient(app, base_url="https://testserver") as client:
        read = client.get(PATH, params={"source_sha256": "b" * 64})
        assert read.status_code == 401
        assert read.json()["code"] == "invalid_session"
        create = client.post(
            PATH,
            headers={
                "Origin": "https://localhost:5173",
                "X-KPDesk-Token": "test-local-owner-" + "x" * 46,
                "X-CSRF-Token": "fake",
            },
            files={"file": ("vendor.csv", CSV, "text/csv")},
            data={"review_json": json.dumps(_payload())},
        )
        assert create.status_code == 401
        assert create.json()["code"] == "invalid_session"
        assert client.get(PATH, params={"source_sha256": "bad"}).status_code == 422


def test_precision_audit_strict_https_and_origin() -> None:
    app = create_app(testing=True, tenant_sessions=True)
    with TestClient(app, base_url="http://testserver") as client:
        assert client.get(PATH, params={"source_sha256": "b" * 64}).status_code == 403
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            PATH,
            headers={"Origin": "https://untrusted.example"},
            files={"file": ("vendor.csv", CSV)},
            data={"review_json": json.dumps(_payload())},
        )
        assert response.status_code == 403
        assert response.json()["code"] == "invalid_origin"


def test_precision_source_content_length_is_bounded_before_processing() -> None:
    app = create_app(testing=True, tenant_sessions=True)
    with TestClient(app, base_url="https://testserver") as client:
        result = client.post(
            PATH,
            headers={
                "Origin": "https://localhost:5173",
                "Content-Length": "100000000",
            },
            files={"file": ("vendor.csv", CSV)},
            data={"review_json": json.dumps(_payload())},
        )
        assert result.status_code == 413
        assert result.json()["code"] == "file_too_large"


def test_precision_preview_requires_actual_session_not_legacy_token() -> None:
    preview_path = PATH + "/preview"
    form = {"mapping_json": json.dumps(_payload()["mapping"]), "start": "0", "limit": "20"}
    for tenant in (False, True):
        app = create_app(testing=True, tenant_sessions=tenant)
        scheme = "https" if tenant else "http"
        with TestClient(app, base_url=scheme + "://testserver") as client:
            reply = client.post(
                preview_path, headers={"Origin": scheme + "://localhost:5173"},
                files={"file": ("vendor.csv", CSV)},
                data=form,
            )
            assert reply.status_code == (401 if tenant else 404)


def test_precision_preview_rejects_oversized_declared_upload() -> None:
    app = create_app(testing=True, tenant_sessions=True)
    with TestClient(app, base_url="https://testserver") as client:
        reply = client.post(
            PATH + "/preview",
            headers={"Origin": "https://localhost:5173", "Content-Length": "200000000"},
            files={"file": ("vendor.csv", CSV)},
            data={"mapping_json": json.dumps(_payload()["mapping"])},
        )
        assert reply.status_code == 413


def test_precision_structure_inspection_is_tenant_only_and_bounded() -> None:
    form = {"formula_policy": "reject"}
    for sessions in (False, True):
        app = create_app(testing=True, tenant_sessions=sessions)
        scheme = "https" if sessions else "http"
        with TestClient(app, base_url=scheme + "://testserver") as client:
            result = client.post(
                PATH + "/inspect",
                headers={"Origin": scheme + "://localhost:5173"},
                files={"file": ("supplier.csv", CSV)},
                data=form,
            )
            assert result.status_code == (401 if sessions else 404)


def test_precision_structure_inspection_enforces_upload_size() -> None:
    app = create_app(testing=True, tenant_sessions=True)
    with TestClient(app, base_url="https://testserver") as client:
        result = client.post(
            PATH + "/inspect",
            headers={"Origin": "https://localhost:5173", "Content-Length": "200000000"},
            files={"file": ("supplier.csv", CSV)},
        )
        assert result.status_code == 413


def test_supplier_mapping_routes_tenant_only_and_http_guards() -> None:
    mapping_url = (
        f"/api/session/organizations/{ORG}/suppliers/vendor-1/mapping-profiles"
    )
    form = {"approval_json": json.dumps({
        "source_sha256": "a" * 64,
        "mapping": {"name": 1, "price": 2, "header_row": 1},
        "price_basis": "standard",
        "evidence": "Reviewed original column headings",
        "explicitly_confirmed": True,
    })}
    with TestClient(create_app(testing=True), base_url="http://testserver") as c:
        assert c.post(
            mapping_url, headers={"Origin": "http://localhost:5173"},
            data=form, files={"file": ("vendor.csv", CSV)},
        ).status_code == 404
    with TestClient(create_app(testing=True, tenant_sessions=True),
                    base_url="https://testserver") as c:
        assert c.post(
            mapping_url, headers={"Origin": "https://localhost:5173"},
            data=form, files={"file": ("vendor.csv", CSV)},
        ).status_code == 401
        assert c.post(
            mapping_url + "/match",
            headers={"Origin": "https://localhost:5173"},
            files={"file": ("vendor.csv", CSV)},
        ).status_code == 401
        too_big = c.post(
            mapping_url, headers={
                "Origin": "https://localhost:5173",
                "Content-Length": "150000000",
            },
            data=form, files={"file": ("vendor.csv", CSV)},
        )
        assert too_big.status_code == 413


def test_tenant_staging_rejects_owner_token_and_missing_session() -> None:
    path = PATH + "/stage"
    mapping = {**_mapping(), "source_sha256": hashlib.sha256(CSV).hexdigest()}
    with TestClient(create_app(testing=True), base_url="http://testserver") as client:
        assert client.post(
            path, headers={"Origin": "http://localhost:5173"},
            files={"file": ("vendor.csv", CSV)},
            data={"mapping_json": json.dumps(mapping)},
        ).status_code == 404
    with TestClient(create_app(testing=True, tenant_sessions=True),
                    base_url="https://testserver") as client:
        denied = client.post(
            path, headers={
                "Origin": "https://localhost:5173",
                "X-CSRF-Token": "not-a-real-session",
                "X-KPDesk-Token": "test-local-owner-" + "x" * 46,
            },
            files={"file": ("vendor.csv", CSV)},
            data={"mapping_json": json.dumps(mapping)},
        )
        assert denied.status_code == 401
        assert denied.json()["code"] == "invalid_session"


def test_tenant_staging_parses_original_with_bounded_pages_without_writes(
    monkeypatch: MonkeyPatch,
) -> None:
    from kpdesk.api import tenant_supplier_precision_routes as routes
    from kpdesk.api.tenant_session_routes import get_unreleased_auth_repository

    # The route's parsing/pagination is exercised in isolation; session/role
    # denial is covered separately by test_tenant_staging_rejects_owner_token.
    monkeypatch.setattr(routes, "_authorized_supplier_write", lambda *_: None)
    app = create_app(testing=True, tenant_sessions=True)
    app.dependency_overrides[get_unreleased_auth_repository] = lambda: object()
    original = (
        b"sku;name;price;currency;unit;pack\n"
        b"A1;Paper;100;RUB;pcs;\n"
        b"A2;Pens;200;RUB;pcs;0\n"
        b"A3;Staples;300;RUB;box;12\n"
    )
    mapping = {**_mapping(), "source_sha256": hashlib.sha256(original).hexdigest()}
    headers = {"Origin": "https://localhost:5173", "X-CSRF-Token": "mock-csrf"}
    with TestClient(app, base_url="https://testserver") as client:
        def request(start: int, *, sha: str | None = None) -> Response:
            expected = dict(mapping)
            if sha is not None:
                expected["source_sha256"] = sha
            reply: Response = client.post(
                PATH + "/stage", headers=headers,
                files={"file": ("supplier.csv", original)},
                data={
                    "mapping_json": json.dumps(expected),
                    "start": str(start), "limit": "1",
                },
            )
            return reply

        first = request(0)
        assert first.status_code == 200, first.text
        data = first.json()
        assert data["offset"] == 0 and data["next_offset"] == 1
        assert data["total_rows"] == 3
        assert data["pack_size_issue_counts"]["missing_pack_size"] == 1
        assert data["pack_size_issue_counts"]["invalid_pack_size"] == 1
        assert data["sampled_rows"][0]["line"] == 2
        assert len(data["sampled_rows"][0]["source_row_sha256"]) == 64
        assert data["sampled_rows"][0]["pack_size_review_reason"]
        assert data["canonical_importable"] is False
        assert data["model_training_authorized"] is False
        assert first.headers["cache-control"] == "no-store"

        last = request(2)
        assert last.status_code == 200, last.text
        assert last.json()["next_offset"] is None
        assert last.json()["sampled_rows"][0]["supplier_sku"] == "A3"
        assert request(0, sha="0" * 64).status_code == 409
        assert request(4).json()["sampled_rows"] == []

        no_digest = client.post(
            PATH + "/stage", headers=headers,
            files={"file": ("supplier.csv", original)},
            data={"mapping_json": json.dumps(_payload()["mapping"])},
        )
        assert no_digest.status_code == 422
        assert no_digest.json()["code"] == "source_digest_required"
        page_limit = client.post(
            PATH + "/stage", headers=headers,
            files={"file": ("supplier.csv", original)},
            data={
                "mapping_json": json.dumps(mapping), "limit": "101",
            },
        )
        assert page_limit.status_code == 422
