"""A68 tenant-reviewed supplier export contract, no live DB or publication."""
from __future__ import annotations

import csv
import io
import uuid
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from kpdesk.api import tenant_supplier_canonical_routes
from kpdesk.api.tenant_session_routes import get_unreleased_auth_repository
from kpdesk.catalog.parse import parse_price_list
from kpdesk.catalog.schema import ImportLimits
from kpdesk.catalog.supplier_canonical_preview import source_row_sha256
from kpdesk.catalog.supplier_staging import SupplierMapping, stage_supplier_prices
from kpdesk.db.tenant_access import AuthenticatedUser
from kpdesk.main import create_app

ORG = uuid.UUID("80000000-0000-4000-8000-000000000001")
SUPPLIER = "sample-supplier"
URL = f"/api/session/organizations/{ORG}/suppliers/{SUPPLIER}/canonical/export"
SOURCE = (
    b"sku;name;price;currency;unit;pack\n"
    b"K-001;Office keyboard;1500.90;RUB;pcs;1\n"
    b"Section: peripherals\n"
)
MAPPING: dict[str, object] = {
    "sku": 0, "name": 1, "price": 2, "currency": 3,
    "sale_unit": 4, "pieces_per_sale_unit": 5, "header_row": 1,
}
STAGED = stage_supplier_prices(
    "supplier.csv", SOURCE,
    SupplierMapping(sku=0, name=1, price=2, currency=3, sale_unit=4,
                    pieces_per_sale_unit=5, header_row=1),
)
ROWS = STAGED.rows


def _body() -> dict[str, object]:
    return {
        "source_sha256": STAGED.sha256,
        "mapping": MAPPING,
        "formula_policy": "reject",
        "price_basis": "standard",
        "explicitly_confirmed_price_basis": True,
        "decisions": [
            {
                "action": "include", "line": ROWS[0].line,
                "source_row_sha256": source_row_sha256(ROWS[0]),
                "confirmed": True, "category": "keyboard",
                "sale_unit": "штука", "pieces_per_sale_unit": 1,
                "attributes": {"connection": "wired", "keys": 104},
            },
            {
                "action": "exclude", "line": ROWS[1].line,
                "source_row_sha256": source_row_sha256(ROWS[1]),
                "confirmed": True,
                "reason": "This line is a supplier section header, not a product.",
            },
        ],
    }


def test_export_complete_reviews_then_existing_catalog_preflight(
    monkeypatch: Any,
) -> None:
    from json import dumps

    # Bypass DB-bound membership for this unit contract; the normal auth
    # boundary is tested separately without this isolated patch.
    monkeypatch.setattr(
        tenant_supplier_canonical_routes, "_authorized_supplier_write",
        lambda *args: SimpleNamespace(identity=AuthenticatedUser(uuid.uuid4())),
    )
    app = create_app(testing=True, tenant_sessions=True)
    app.dependency_overrides[get_unreleased_auth_repository] = lambda: object()
    headers = {"Origin": "https://localhost:5173", "X-CSRF-Token": "test"}
    with TestClient(app, base_url="https://testserver") as client:
        def export(
            payload: dict[str, object], source: bytes = SOURCE,
        ) -> Any:
            return client.post(
                URL, headers=headers, data={"review_json": dumps(payload)},
                files={"file": ("supplier.csv", source, "text/csv")},
            )

        accepted = export(_body())
        assert accepted.status_code == 200, accepted.text
        assert accepted.headers["cache-control"] == "no-store"
        assert "x-catalog-sha256" in accepted.headers[
            "access-control-expose-headers"
        ].lower()
        assert accepted.headers["x-source-sha256"] == STAGED.sha256
        assert accepted.headers["x-excluded-rows"] == str(ROWS[1].line)
        csv_data = accepted.content
        parsed = parse_price_list("reviewed-supplier.csv", csv_data, ImportLimits())
        assert parsed.publishable is True
        assert [item.sku for item in parsed.products] == ["K-001"]
        assert csv.reader(io.StringIO(csv_data.decode()), delimiter=";")

        truncated = _body()
        truncated["decisions"] = _body()["decisions"][:1]  # type: ignore[index]
        assert export(truncated).status_code == 422

        changed_source = export(_body(), SOURCE.replace(b"K-001", b"K-002"))
        assert changed_source.status_code == 409
        assert changed_source.json()["code"] == "source_changed"

        bad_row = _body()
        bad_row["decisions"][0]["source_row_sha256"] = "0" * 64  # type: ignore[index]
        assert export(bad_row).status_code == 422

        unchecked = _body()
        unchecked["decisions"][0]["confirmed"] = False  # type: ignore[index]
        assert export(unchecked).status_code == 422

        no_explanation = _body()
        no_explanation["decisions"][1]["reason"] = "skip"  # type: ignore[index]
        assert export(no_explanation).status_code == 422


def test_export_requires_https_session_before_any_supplier_preview() -> None:
    app = create_app(testing=True, tenant_sessions=True)
    with TestClient(app, base_url="https://testserver") as client:
        result = client.post(
            URL,
            headers={"Origin": "https://localhost:5173", "X-CSRF-Token": "invalid"},
            data={"review_json": "{}"},
            files={"file": ("supplier.csv", SOURCE, "text/csv")},
        )
        assert result.status_code == 401
        assert result.json()["code"] == "invalid_session"


def test_canonical_export_isolated_from_legacy_app_and_untrusted_origin() -> None:
    import json

    payload = json.dumps(_body())
    with TestClient(create_app(testing=True), base_url="http://testserver") as client:
        legacy = client.post(
            URL, headers={"Origin": "http://localhost:5173"},
            data={"review_json": payload},
            files={"file": ("supplier.csv", SOURCE, "text/csv")},
        )
        assert legacy.status_code == 404
    with TestClient(create_app(testing=True, tenant_sessions=True),
                    base_url="http://testserver") as client:
        plain = client.post(
            URL, headers={"Origin": "https://localhost:5173"},
            data={"review_json": payload},
            files={"file": ("supplier.csv", SOURCE, "text/csv")},
        )
        assert plain.status_code == 403
        assert plain.json()["code"] == "https_required"
    with TestClient(create_app(testing=True, tenant_sessions=True),
                    base_url="https://testserver") as client:
        cross_site = client.post(
            URL, headers={"Origin": "https://untrusted.example"},
            data={"review_json": payload},
            files={"file": ("supplier.csv", SOURCE, "text/csv")},
        )
        assert cross_site.status_code == 403
        assert cross_site.json()["code"] == "invalid_origin"
        oversized = client.post(
            URL, headers={
                "Origin": "https://localhost:5173",
                "Content-Length": "90000000",
            },
            data={"review_json": payload},
            files={"file": ("supplier.csv", SOURCE, "text/csv")},
        )
        assert oversized.status_code == 413
        assert oversized.json()["code"] == "file_too_large"
        cors = client.options(
            URL,
            headers={
                "Origin": "https://localhost:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-csrf-token",
            },
        )
        assert cors.status_code == 200, cors.text
        assert "x-csrf-token" in cors.headers["access-control-allow-headers"].lower()
