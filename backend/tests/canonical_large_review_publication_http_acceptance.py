"""A90: real tenant HTTPS publish of 121 independently reviewed source rows.

A75 has already committed three paged revisions. A79/A85 has installed
the signed provenance contract. Run ONLY on marked disposable SQL clone.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any, cast

from canonical_checkpoint_acceptance import PUT
from canonical_checkpoint_paged_http_acceptance import (
    MAPPING,
    ORIGINAL,
    STAGED,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Engine, text

from kpdesk.api.tenant_session_routes import SESSION_COOKIE
from kpdesk.api.tenant_supplier_canonical_routes import reviewed_decisions
from kpdesk.api.tenant_supplier_checkpoint_routes import SaveDecision
from kpdesk.catalog.review_signing import ReviewSigningCredential
from kpdesk.catalog.supplier_canonical_preview import preview_canonical_catalog
from kpdesk.catalog.supplier_staging import SupplierMapping, mapping_digest
from kpdesk.db.auth_sessions import IssuedSession
from kpdesk.db.authenticated_tenant import bind_authenticated_tenant
from kpdesk.db.tenant_access import AuthenticatedUser
from kpdesk.matching.profiles import TAXONOMY_VERSION


def verify_large_review_publication_https(
    tenant: Engine,
    owner: Engine,
    organization: uuid.UUID,
    outsider: uuid.UUID,
    reviewer: uuid.UUID,
    session: IssuedSession,
    factory: Callable[[str], FastAPI],
    signing_credential: ReviewSigningCredential,
) -> None:
    supplier = "supplier-a75"
    mapping = SupplierMapping(
        sku=0, name=1, price=2, currency=3, category=4,
        sale_unit=5, pieces_per_sale_unit=6, header_row=1,
    )
    selector: dict[str, object] = {
        "org": organization, "supplier": supplier, "source": STAGED.sha256,
        "mapping": mapping_digest(mapping, "reject"),
        "taxonomy": TAXONOMY_VERSION, "basis": "standard",
    }
    with owner.connect() as connection:
        head = connection.execute(text("""
            SELECT checkpoint_id,reviewer_user_id,total_count
            FROM public.a77_current_head
            WHERE organization_id=:org AND supplier_id=:supplier
              AND source_sha256=:source AND mapping_sha256=:mapping
              AND taxonomy_version=:taxonomy AND price_basis=:basis
        """), selector).one()
        raw_rows = cast(list[dict[str, Any]], list(connection.execute(text("""
            SELECT decision FROM public.a77_current_row
            WHERE organization_id=:org AND supplier_id=:supplier
              AND source_sha256=:source AND mapping_sha256=:mapping
              AND taxonomy_version=:taxonomy AND price_basis=:basis
            ORDER BY source_line
        """), selector).scalars().all()))
        assert head.reviewer_user_id == reviewer
        assert head.total_count == len(raw_rows) == len(STAGED.rows) == 121

    decisions = [SaveDecision.model_validate(value).root for value in raw_rows]
    preview = preview_canonical_catalog(
        STAGED, reviewed_decisions(decisions), organization_id=organization,
        reviewer=AuthenticatedUser(reviewer), expected_source_sha256=STAGED.sha256,
        price_basis="standard", explicitly_confirmed_price_basis=True,
    )
    assert preview.products_count == 120
    body = {
        "source_sha256": STAGED.sha256,
        "mapping": MAPPING, "formula_policy": "reject", "price_basis": "standard",
    }
    root = f"/api/session/organizations/{organization}"
    target = (
        f"{root}/suppliers/{supplier}/canonical/checkpoints/"
        f"{head.checkpoint_id}/publish"
    )
    headers = {
        "Origin": "https://testserver", "X-CSRF-Token": session.csrf_token,
        "X-Catalog-SHA256": preview.csv_sha256,
        "Idempotency-Key": "a90-large-121-verified-publish",
    }

    def send(
        client: TestClient, *, endpoint: str = target,
        request_body: dict[str, Any] = body,
        fields: dict[str, str] = headers,
        source: bytes = ORIGINAL,
    ) -> Response:
        return cast(Response, client.post(
            endpoint, headers=fields,
            files={"file": ("paging.csv", source, "text/csv")},
            data={"review_json": json.dumps(request_body)},
        ))

    app = factory("https://testserver")
    app.state.a85_review_signing_credential = signing_credential
    with TestClient(app, base_url="https://testserver") as client:
        assert send(client).status_code == 401
        client.cookies.set(SESSION_COOKIE, session.session_token)
        assert send(client, endpoint=target.replace(
            str(organization), str(outsider),
        )).status_code == 403
        assert send(client, source=ORIGINAL.replace(b"PG-0001", b"PG-9001")
                    ).status_code == 409
        assert send(client, fields={
            **headers, "Idempotency-Key": "a90-wrong-csv-sha",
            "X-Catalog-SHA256": "e" * 64,
        }).status_code == 409
        assert send(client, endpoint=target.replace(
            str(head.checkpoint_id), str(uuid.uuid4()),
        )).status_code == 409

        # The published digest is always derived from the same full 121-line
        # canonical export; no browser-supplied decision list reaches publish.
        created = send(client)
        assert created.status_code == 201, created.text
        published = uuid.UUID(created.json()["version_id"])
        assert created.json()["source_sha256"] == preview.csv_sha256
        assert created.json()["rows_count"] == 120
        assert created.headers["cache-control"] == "no-store"
        repeated = send(client)
        assert repeated.status_code == 201, repeated.text
        assert repeated.json()["version_id"] == str(published)
        assert client.get(root + "/catalog/current").json()["version_id"] == str(published)
        with owner.connect() as connection:
            assert connection.scalar(text("""
                SELECT count(*) FROM public.a79_catalog_review_provenance
                WHERE organization_id=:org AND catalog_version_id=:version
                  AND checkpoint_id=:checkpoint
                  AND canonical_csv_sha256=:digest
            """), {
                "org": organization, "version": published,
                "checkpoint": head.checkpoint_id, "digest": preview.csv_sha256,
            }) == 1

        # A separate incomplete source-bound chain (120 out of 121 rows)
        # must not reserve a publish key or change the active catalog.
        incomplete_scope = {**selector, "supplier": "supplier-a90-incomplete"}
        prior: uuid.UUID | None = None
        for n, batch in enumerate((raw_rows[:50], raw_rows[50:100], raw_rows[100:120]), 1):
            with tenant.begin() as connection:
                bind_authenticated_tenant(
                    connection, session_token=session.session_token,
                    organization_id=organization, operation="write",
                    csrf=session.csrf_token,
                )
                saved = connection.scalar(PUT, {
                    **incomplete_scope, "idempotency": f"a90-incomplete-part-{n}",
                    "expected": prior, "decisions": batch,
                })
                assert isinstance(saved, uuid.UUID)
                prior = saved
        assert prior is not None
        incomplete = target.replace(supplier, "supplier-a90-incomplete").replace(
            str(head.checkpoint_id), str(prior),
        )
        rejected = send(client, endpoint=incomplete, fields={
            **headers, "Idempotency-Key": "a90-incomplete-publish",
        })
        assert rejected.status_code == 409, rejected.text
        assert client.get(root + "/catalog/current").json()["version_id"] == str(published)
        with owner.connect() as connection:
            assert connection.scalar(text("""
                SELECT count(*) FROM public.tenant_catalog_idempotency
                WHERE organization_id=:org AND key='a90-incomplete-publish'
            """), {"org": organization}) == 0

        # Append a new immutable revision of the previously published supplier
        # review. The old head is no longer publishable under another key.
        with tenant.begin() as connection:
            bind_authenticated_tenant(
                connection, session_token=session.session_token,
                organization_id=organization, operation="write",
                csrf=session.csrf_token,
            )
            fresh_head = connection.scalar(PUT, {
                **selector, "expected": head.checkpoint_id,
                "idempotency": "a90-new-head-after-publish",
                "decisions": raw_rows[:1],
            })
            assert isinstance(fresh_head, uuid.UUID)
            assert fresh_head != head.checkpoint_id
        stale = send(client, fields={
            **headers, "Idempotency-Key": "a90-stale-head-publish",
        })
        assert stale.status_code == 409
        with owner.connect() as connection:
            assert connection.scalar(text("""
                SELECT count(*) FROM public.tenant_catalog_idempotency
                WHERE organization_id=:org AND key='a90-stale-head-publish'
            """), {"org": organization}) == 0
