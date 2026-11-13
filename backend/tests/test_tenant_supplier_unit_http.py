"""A31 tenant-only supplier unit audit HTTP contract and fail-closed controls."""
from __future__ import annotations

import hashlib
import json
import uuid

from fastapi.testclient import TestClient
from httpx import Response
from pytest import MonkeyPatch

from kpdesk.api import tenant_supplier_unit_routes as routes
from kpdesk.api.tenant_session_routes import get_unreleased_auth_repository
from kpdesk.main import create_app

ORG = uuid.UUID("9f000000-0000-4000-8000-000000000001")
PATH = f"/api/session/organizations/{ORG}/suppliers/vendor-1/unit-reviews"
SOURCE = b"sku;name;price;currency;unit;pack\nA-1;Office item;250;RUB;pcs;\n"
ORIGINAL_SHA = hashlib.sha256(SOURCE).hexdigest()
EVIDENCE = (
    "I checked the original supplier tariff and independently verified "
    "the listed sale denominator and package size with the source document."
)


def _decision(*, digest: str = ORIGINAL_SHA, row_sha: str = "b" * 64) -> dict[str, object]:
    return {
        "source_sha256": digest, "source_row_sha256": row_sha,
        "line": 2, "mapping": {
            "sku": 0, "name": 1, "price": 2, "currency": 3,
            "sale_unit": 4, "pieces_per_sale_unit": 5, "header_row": 1,
        },
        "finding": "confirm_price_unit",
        "approved_sale_unit": "штука", "approved_pack_size": 1,
        "rationale": EVIDENCE, "personally_reviewed": True,
    }


def test_unit_routes_not_available_to_legacy_or_anonymous_tenant() -> None:
    with TestClient(create_app(testing=True)) as client:
        assert client.get(PATH, params={"source_sha256": ORIGINAL_SHA}).status_code == 404
        assert client.post(PATH, headers={"Origin": "http://localhost:5173"},
                           files={"file": ("vendor.csv", SOURCE)},
                           data={"review_json": json.dumps(_decision())}).status_code == 404
    with TestClient(
        create_app(testing=True, tenant_sessions=True),
        base_url="https://testserver",
    ) as client:
        read = client.get(PATH, params={"source_sha256": ORIGINAL_SHA})
        assert read.status_code == 401
        write = client.post(PATH, headers={
            "Origin": "https://localhost:5173",
            "X-CSRF-Token": "fake",
            "X-KPDesk-Token": "test-local-owner-" + "x" * 46,
        }, files={"file": ("vendor.csv", SOURCE)},
            data={"review_json": json.dumps(_decision())})
        assert write.status_code == 401
        assert write.json()["code"] == "invalid_session"


def test_unit_review_requires_https_origin_and_upload_bounds() -> None:
    with TestClient(
        create_app(testing=True, tenant_sessions=True),
        base_url="http://testserver",
    ) as client:
        assert client.get(PATH, params={"source_sha256": ORIGINAL_SHA}).status_code == 403
    with TestClient(
        create_app(testing=True, tenant_sessions=True),
        base_url="https://testserver",
    ) as client:
        bad_origin = client.post(PATH, headers={"Origin": "https://outside.example"},
            files={"file": ("vendor.csv", SOURCE)},
            data={"review_json": json.dumps(_decision())})
        assert bad_origin.status_code == 403
        oversized = client.post(PATH, headers={
            "Origin": "https://localhost:5173", "Content-Length": "100000000",
        }, files={"file": ("vendor.csv", SOURCE)},
            data={"review_json": json.dumps(_decision())})
        # Missing session is rejected before parsing the uploaded bytes.
        assert oversized.status_code == 401


def test_unit_route_checks_actual_file_and_row_before_any_attestation(
    monkeypatch: MonkeyPatch,
) -> None:
    from kpdesk.catalog.supplier_canonical_preview import source_row_sha256
    from kpdesk.catalog.supplier_staging import SupplierMapping, stage_supplier_prices

    source = stage_supplier_prices("vendor.csv", SOURCE, SupplierMapping(
        sku=0, name=1, price=2, currency=3, sale_unit=4,
        pieces_per_sale_unit=5, header_row=1,
    ))
    digest = source_row_sha256(source.rows[0])
    called: list[tuple[str, str]] = []

    class StubUnitStore:
        def __init__(self, _: object) -> None:
            pass

        def attest(self, staging: object, supplier_id: str, row: object, **kwargs: object) -> bool:
            called.append((supplier_id, str(kwargs["finding"])))
            return True

    monkeypatch.setattr(routes, "_authorized_supplier_write", lambda *_: object())
    monkeypatch.setattr(routes, "TenantSupplierUnitReviewStore", StubUnitStore)
    app = create_app(testing=True, tenant_sessions=True)
    app.dependency_overrides[get_unreleased_auth_repository] = lambda: object()
    headers = {"Origin": "https://localhost:5173", "X-CSRF-Token": "mock"}
    with TestClient(app, base_url="https://testserver") as client:
        def post(decision: dict[str, object]) -> Response:
            result: Response = client.post(PATH, headers=headers,
                files={"file": ("vendor.csv", SOURCE)},
                data={"review_json": json.dumps(decision)})
            return result

        wrong_file = post(_decision(digest="0" * 64, row_sha=digest))
        assert wrong_file.status_code == 409
        wrong_row = post(_decision(row_sha="0" * 64))
        assert wrong_row.status_code == 409
        unsigned = _decision(row_sha=digest)
        unsigned.pop("personally_reviewed")
        assert post(unsigned).status_code == 422
        wrong_finding = _decision(row_sha=digest)
        wrong_finding["finding"] = "approve_rounding"
        assert post(wrong_finding).status_code == 422
        assert called == []
        success = post(_decision(row_sha=digest))
        assert success.status_code == 201, success.text
        assert success.json()["source_row_sha256"] == digest
        assert success.json()["recorded"] is True
        assert success.json()["published"] is False
        assert success.json()["model_training_authorized"] is False
        assert success.headers["cache-control"] == "no-store"
        assert called == [("vendor-1", "confirm_price_unit")]
