"""A67 authenticated preflight contract against mock storage: no database writes."""

from __future__ import annotations

import csv
import io
import uuid
from typing import Any

from fastapi.testclient import TestClient

from kpdesk.api import tenant_session_routes
from kpdesk.api.tenant_session_routes import get_unreleased_auth_repository
from kpdesk.catalog.schema import COLUMNS_V2
from kpdesk.db.tenant_access import AuthenticatedUser
from kpdesk.main import create_app


def test_preflight_valid_invalid_and_changed_source_never_publish_implicitly(
    monkeypatch: Any,
) -> None:
    published: list[str] = []

    class FakeCatalog:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def _authorize(self, operation: str) -> None:
            assert operation == "write"

        def unavailable_categories(self, products: tuple[object, ...]) -> tuple[str, ...]:
            # A67 substitutes the repository; its synthetic paper category
            # is explicitly supported by the released database constraint.
            assert len(products) == 1
            assert getattr(products[0], "category", None) == "paper"
            return ()

        def publish(self, *args: object) -> None:
            published.append("unexpected publish")

    monkeypatch.setattr(tenant_session_routes, "TenantCatalogRepository", FakeCatalog)
    app = create_app(testing=True, tenant_sessions=True)

    class FakeRepository:
        connection = None

        def authenticate(
            self, token: str, *, operation: str, csrf: str | None = None,
        ) -> AuthenticatedUser:
            assert operation == "write"
            assert csrf == "test-csrf"
            return AuthenticatedUser(uuid.uuid4())

    app.dependency_overrides[get_unreleased_auth_repository] = FakeRepository
    organization = uuid.uuid4()
    prefix = f"/api/session/organizations/{organization}/catalog"
    headers = {"Origin": "https://localhost:5173", "X-CSRF-Token": "test-csrf"}

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter=";", lineterminator="\n")
    writer.writerow(COLUMNS_V2)
    row = {
        "sku": "P-001", "name": "Бумага А4 80", "category": "paper",
        "sale_unit": "пачка", "pieces_per_sale_unit": "1", "price_rub": "399.90",
        "paper_format": "A4", "paper_gsm": "80", "attributes_json": "{}",
    }
    writer.writerow([row.get(column, "") for column in COLUMNS_V2])
    valid = buffer.getvalue().encode("utf-8")

    with TestClient(app, base_url="https://testserver") as client:
        cors = client.options(
            prefix + "/import",
            headers={
                "Origin": "https://localhost:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-csrf-token,x-source-sha256",
            },
        )
        assert cors.status_code == 200, cors.text
        assert "x-source-sha256" in cors.headers["access-control-allow-headers"].lower()
        accepted = client.post(
            prefix + "/preflight", headers=headers,
            files={"file": ("catalog.csv", valid, "text/csv")},
        )
        assert accepted.status_code == 200, accepted.text
        result = accepted.json()
        assert result["publishable"] is True
        assert result["rows_count"] == 1
        assert result["total_errors"] == 0
        assert accepted.headers["cache-control"] == "no-store"

        invalid = client.post(
            prefix + "/preflight", headers=headers,
            files={"file": ("wrong.csv", b"sku;price\n1;-5\n", "text/csv")},
        )
        assert invalid.status_code == 200, invalid.text
        rejected = invalid.json()
        assert rejected["publishable"] is False
        assert rejected["rows_count"] == 0
        assert rejected["errors"][0]["code"] == "invalid_header"
        assert rejected["errors"][0]["row"] == 1

        changed = client.post(
            prefix + "/import",
            headers={**headers, "Idempotency-Key": "a67-test",
                     "X-Source-SHA256": result["source_sha256"]},
            files={"file": ("wrong.csv", b"sku;price\n1;-5\n", "text/csv")},
        )
        assert changed.status_code == 409
        assert changed.json()["code"] == "source_changed"
        assert published == []
