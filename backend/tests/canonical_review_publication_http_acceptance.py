"""A82 HTTPS test: only clone routes may publish an immutable supplier review."""
from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import asdict
from typing import cast

from canonical_checkpoint_acceptance import MAP, SOURCE, STAGED
from canonical_review_publication_clone_schema import rotate_review_signer
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Engine, text

from kpdesk.api.tenant_session_routes import SESSION_COOKIE
from kpdesk.catalog.review_signing import ReviewSigningCredential
from kpdesk.db.auth_sessions import IssuedSession


def verify_review_publication_http(
    factory: Callable[[str], FastAPI],
    organization: uuid.UUID,
    outsider: uuid.UUID,
    checkpoint: uuid.UUID,
    session: IssuedSession,
    canonical_sha256: str,
    signing_credential: ReviewSigningCredential,
    owner: Engine,
) -> tuple[uuid.UUID, uuid.UUID, ReviewSigningCredential]:
    root = f"/api/session/organizations/{organization}"
    path = f"{root}/suppliers/supplier-a79/canonical/checkpoints/{checkpoint}/publish"
    body = json.dumps({
        "source_sha256": STAGED.sha256, "mapping": asdict(MAP),
        "formula_policy": "reject", "price_basis": "standard",
    })
    headers = {
        "Origin": "https://testserver",
        "X-CSRF-Token": session.csrf_token,
        "Idempotency-Key": "a82-verified-http-publish",
        "X-Catalog-SHA256": canonical_sha256,
    }

    def post(
        client: TestClient, *, target: str = path,
        payload: bytes = SOURCE, fields: dict[str, str] | None = None,
    ) -> Response:
        return cast(Response, client.post(
            target, headers=fields or headers,
            files={"file": ("supplier.csv", payload, "text/csv")},
            data={"review_json": body},
        ))

    app = factory("https://testserver")
    with TestClient(app, base_url="https://testserver") as client:
        assert post(client).status_code == 401
        client.cookies.set(SESSION_COOKIE, session.session_token)
        assert post(client).status_code == 503
        app.state.a85_review_signing_credential = signing_credential
        assert post(client, target=path.replace(str(organization), str(outsider))).status_code == 403
        assert post(client, fields={**headers, "X-CSRF-Token": "invalid"}).status_code == 401
        assert post(client, payload=SOURCE + b"\n").status_code == 409
        assert post(client, target=path.replace(str(checkpoint), str(uuid.uuid4()))).status_code == 409
        assert post(client, fields={**headers, "X-Catalog-SHA256": "f" * 64}).status_code == 409
        # A84: a 32-byte key is not sufficient; it must match the protected DB key.
        app.state.a85_review_signing_credential = ReviewSigningCredential(
            epoch=signing_credential.epoch, key=bytes(32),
        )
        assert post(client, fields={
            **headers, "Idempotency-Key": "a84-incorrect-server-signature",
        }).status_code == 409
        app.state.a85_review_signing_credential = signing_credential
        current = client.get(root + "/catalog/current")
        current_before = current.json()["version_id"] if current.status_code == 200 else None
        created = post(client)
        assert created.status_code == 201, created.text
        published = uuid.UUID(created.json()["version_id"])
        assert created.json()["source_sha256"] == canonical_sha256, created.json()
        assert created.json()["rows_count"] == 2
        assert created.headers["cache-control"] == "no-store"
        assert current_before is None or published != uuid.UUID(current_before)
        duplicate = post(client)
        assert duplicate.status_code == 201, duplicate.text
        assert duplicate.json()["version_id"] == str(published)
        current = client.get(root + "/catalog/current")
        assert current.status_code == 200
        assert current.json()["version_id"] == str(published)

        # A85: rotation is committed by the clone schema owner, not by an
        # HTTP user. Old workers fail closed; past provenance stays immutable.
        with owner.begin() as connection:
            rotated = rotate_review_signer(connection)
        assert rotated.epoch == signing_credential.epoch + 1
        assert rotated.key != signing_credential.key
        assert post(client, fields={
            **headers, "Idempotency-Key": "a85-stale-worker-denied",
        }).status_code == 409
        assert client.get(root + "/catalog/current").json()["version_id"] == str(published)
        app.state.a85_review_signing_credential = ReviewSigningCredential(
            epoch=signing_credential.epoch, key=rotated.key,
        )
        assert post(client, fields={
            **headers, "Idempotency-Key": "a85-spoofed-key-epoch",
        }).status_code == 409

        app.state.a85_review_signing_credential = rotated
        after_rotation = post(client, fields={
            **headers, "Idempotency-Key": "a85-new-epoch-publish",
        })
        assert after_rotation.status_code == 201, after_rotation.text
        rotated_id = uuid.UUID(after_rotation.json()["version_id"])
        assert rotated_id != published
        assert post(client, fields={
            **headers, "Idempotency-Key": "a85-new-epoch-publish",
        }).json()["version_id"] == str(rotated_id)
        assert client.get(root + "/catalog/current").json()["version_id"] == str(rotated_id)
        with owner.connect() as connection:
            assert connection.scalar(
                text("SELECT count(*) FROM public.a84_provenance_signing_secret")
            ) == 2
            assert connection.scalar(text("""
                SELECT count(*) FROM public.a79_catalog_review_provenance
                WHERE organization_id=:org
                  AND catalog_version_id IN (:previous,:latest)
            """), {"org": organization, "previous": published, "latest": rotated_id}) == 2
        return published, rotated_id, rotated
