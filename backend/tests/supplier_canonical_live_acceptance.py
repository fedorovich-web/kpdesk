"""A69: source-bound reviewed catalog through HTTPS tenant roles on an isolated clone."""
from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from typing import Any, cast

from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from kpdesk.api.tenant_session_routes import SESSION_COOKIE
from kpdesk.catalog.supplier_canonical_preview import source_row_sha256
from kpdesk.catalog.supplier_staging import SupplierMapping, stage_supplier_prices
from kpdesk.db.auth_sessions import IssuedSession

_SOURCE = (
    b"sku;name;price;currency;unit;pack\n"
    b"KEY-100;Office keyboard;1500.90;RUB;pcs;1\n"
    b"Section: peripherals\n"
)
_MAPPING = {
    "sku": 0, "name": 1, "price": 2, "currency": 3,
    "sale_unit": 4, "pieces_per_sale_unit": 5, "header_row": 1,
}
_STAGED = stage_supplier_prices(
    "vendor.csv", _SOURCE, SupplierMapping(
        sku=0, name=1, price=2, currency=3, sale_unit=4,
        pieces_per_sale_unit=5, header_row=1,
    ),
)


def verify_reviewed_catalog_tenant_https(
    factory: Callable[[str], FastAPI],
    organization: uuid.UUID,
    outsider: uuid.UUID,
    owner_session: IssuedSession,
    editor_session: IssuedSession,
) -> uuid.UUID:
    """Prove each HTTP stage without a privilege bypass or direct DB publication."""
    prefix = f"/api/session/organizations/{organization}"
    outsider_prefix = f"/api/session/organizations/{outsider}"
    export_path = prefix + "/suppliers/vendor-a69/canonical/export"
    headers = {
        "Origin": "https://testserver", "X-CSRF-Token": owner_session.csrf_token,
    }
    review: dict[str, Any] = {
        "source_sha256": _STAGED.sha256,
        "mapping": _MAPPING,
        "formula_policy": "reject",
        "price_basis": "standard",
        "explicitly_confirmed_price_basis": True,
        "decisions": [
            {
                "action": "include", "line": _STAGED.rows[0].line,
                "source_row_sha256": source_row_sha256(_STAGED.rows[0]),
                "confirmed": True, "category": "keyboard",
                "sale_unit": "штука", "pieces_per_sale_unit": 1,
                "attributes": {"connection": "wired", "keys": 104},
            },
            {
                "action": "exclude", "line": _STAGED.rows[1].line,
                "source_row_sha256": source_row_sha256(_STAGED.rows[1]),
                "confirmed": True,
                "reason": "This is a supplier section heading and not a product.",
            },
        ],
    }

    def export(client: TestClient, body: dict[str, Any], *, path: str = export_path,
               csrf: str | None = None) -> Response:
        return cast(Response, client.post(
            path, headers={
                **headers, **({"X-CSRF-Token": csrf} if csrf is not None else {}),
            },
            files={"file": ("vendor.csv", _SOURCE, "text/csv")},
            data={"review_json": json.dumps(body)},
        ))

    with TestClient(factory("https://testserver"), base_url="https://testserver") as client:
        untrusted = export(client, review)
        assert untrusted.status_code == 401
        client.cookies.set(SESSION_COOKIE, owner_session.session_token)

        denied = export(client, review, path=export_path.replace(str(organization), str(outsider)))
        assert denied.status_code == 403, denied.text
        assert export(client, review, csrf="invalid-csrf").status_code == 401

        excluded = {**review, "decisions": review["decisions"][:1]}
        incomplete = export(client, excluded)
        assert incomplete.status_code == 422
        assert incomplete.json()["code"] in (
            "approval_or_source_mismatch", "missing_or_unknown_review_decision",
        )

        created = export(client, review)
        assert created.status_code == 200, created.text
        assert created.headers["cache-control"] == "no-store"
        csv_content = created.content
        assert created.headers["x-catalog-sha256"] == hashlib.sha256(csv_content).hexdigest()
        assert created.headers["x-source-sha256"] == _STAGED.sha256
        assert created.headers["x-excluded-rows"] == str(_STAGED.rows[1].line)

        before = client.get(prefix + "/catalog/current")
        before_catalog = before.json()["version_id"] if before.status_code == 200 else None
        assert before.status_code in (200, 404)
        preview = client.post(
            prefix + "/catalog/preflight", headers=headers,
            files={"file": ("reviewed-supplier.csv", csv_content, "text/csv")},
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["publishable"] is True
        assert preview.json()["rows_count"] == 1
        assert preview.json()["total_errors"] == 0
        assert preview.json()["source_sha256"] == created.headers["x-catalog-sha256"]
        check = client.get(prefix + "/catalog/current")
        assert check.status_code == before.status_code
        if before_catalog is not None:
            assert check.json()["version_id"] == before_catalog
        assert client.post(
            outsider_prefix + "/catalog/preflight", headers=headers,
            files={"file": ("reviewed-supplier.csv", csv_content, "text/csv")},
        ).status_code == 403
        changed = client.post(
            prefix + "/catalog/import",
            headers={
                **headers,
                "Idempotency-Key": "a69-source-mismatch",
                "X-Source-SHA256": created.headers["x-catalog-sha256"],
            },
            files={"file": ("reviewed-supplier.csv", csv_content + b"\n", "text/csv")},
        )
        assert changed.status_code == 409, changed.text
        assert changed.json()["code"] == "source_changed"
        assert client.get(prefix + "/catalog/current").status_code == check.status_code

        confirmed = client.post(
            prefix + "/catalog/import",
            headers={
                **headers,
                "Idempotency-Key": "a69-reviewed-catalog-publish",
                "X-Source-SHA256": created.headers["x-catalog-sha256"],
            },
            files={"file": ("reviewed-supplier.csv", csv_content, "text/csv")},
        )
        assert confirmed.status_code == 201, confirmed.text
        repeated = client.post(
            prefix + "/catalog/import",
            headers={
                **headers,
                "Idempotency-Key": "a69-reviewed-catalog-publish",
                "X-Source-SHA256": created.headers["x-catalog-sha256"],
            },
            files={"file": ("reviewed-supplier.csv", csv_content, "text/csv")},
        )
        assert repeated.status_code == 201, repeated.text
        assert repeated.json()["version_id"] == confirmed.json()["version_id"]
        active = client.get(prefix + "/catalog/current")
        assert active.status_code == 200, active.text
        assert active.json()["version_id"] == confirmed.json()["version_id"]
        assert active.json()["source_sha256"] == created.headers["x-catalog-sha256"]
        assert active.json()["rows_count"] == 1
        assert client.get(outsider_prefix + "/catalog/current").status_code == 403
        published_id = uuid.UUID(active.json()["version_id"])

        with TestClient(factory("https://testserver"), base_url="https://testserver") as editor:
            editor.cookies.set(SESSION_COOKIE, editor_session.session_token)
            assert editor.get(prefix + "/catalog/current").status_code == 200
        return published_id
