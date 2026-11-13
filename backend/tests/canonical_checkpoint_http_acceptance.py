"""A73 source-bound HTTP checkpoint save/restore on the A72 marked clone."""
from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from typing import Any, cast

from canonical_checkpoint_acceptance import SOURCE, STAGED, _decisions
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from kpdesk.api.tenant_session_routes import SESSION_COOKIE
from kpdesk.db.auth_sessions import IssuedSession

MAPPING = {
    "sku": 0, "name": 1, "price": 2, "currency": 3,
    "category": 4, "sale_unit": 5, "pieces_per_sale_unit": 6,
    "header_row": 1,
}


def verify_checkpoint_http_contract(
    factory: Callable[[str], FastAPI],
    organization: uuid.UUID,
    outsider: uuid.UUID,
    owner: IssuedSession,
    editor: IssuedSession,
    reviewer: uuid.UUID,
) -> None:
    prefix = (
        f"/api/session/organizations/{organization}"
        "/suppliers/supplier-a73/canonical/checkpoints"
    )
    outsider_url = prefix.replace(str(organization), str(outsider))
    decisions = [_decisions()[0], _decisions()[2]]
    save: dict[str, Any] = {
        "source_sha256": STAGED.sha256, "mapping": MAPPING,
        "formula_policy": "reject", "price_basis": "standard",
        "explicitly_confirmed_price_basis": True,
        "idempotency_key": "a73-checkpoint-0001",
        "expected_checkpoint_id": None,
        "decisions": decisions,
    }
    restore: dict[str, Any] = {
        "source_sha256": STAGED.sha256, "mapping": MAPPING,
        "formula_policy": "reject", "price_basis": "standard",
    }

    def request(
        client: TestClient, endpoint: str, data: dict[str, Any],
        token: str, *, source: bytes = SOURCE,
    ) -> Response:
        return cast(Response, client.post(
            endpoint,
            headers={
                "Origin": "https://testserver",
                "X-CSRF-Token": token,
            },
            data={"review_json": json.dumps(data)},
            files={"file": ("supplier.csv", source, "text/csv")},
        ))

    with TestClient(factory("https://testserver"), base_url="https://testserver") as client:
        denied = request(client, prefix + "/save", save, owner.csrf_token)
        assert denied.status_code == 401, denied.text
        client.cookies.set(SESSION_COOKIE, owner.session_token)

        cross = request(client, outsider_url + "/save", save, owner.csrf_token)
        assert cross.status_code == 403, cross.text
        assert request(client, prefix + "/save", save, "wrong-csrf").status_code == 401

        changed = request(
            client, prefix + "/save", save, owner.csrf_token,
            source=SOURCE.replace(b"KB-01", b"KB-99"),
        )
        assert changed.status_code == 409, changed.text
        assert changed.json()["code"] == "source_changed"

        fraudulent = dict(save)
        fraudulent["decisions"] = [
            {**decisions[0], "source_row_sha256": "0" * 64},
        ]
        rejected = request(client, prefix + "/save", fraudulent, owner.csrf_token)
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["code"] == "source_row_changed"

        unsigned = dict(save)
        unsigned["decisions"] = [{**decisions[0], "confirmed": False}]
        assert request(client, prefix + "/save", unsigned, owner.csrf_token).status_code == 422

        recorded = request(client, prefix + "/save", save, owner.csrf_token)
        assert recorded.status_code == 201, recorded.text
        first = uuid.UUID(recorded.json()["checkpoint_id"])
        assert recorded.json()["published"] is False
        assert recorded.headers["cache-control"] == "no-store"
        assert recorded.json()["mapping_sha256"] != hashlib.sha256(b"").hexdigest()

        repeated = request(client, prefix + "/save", save, owner.csrf_token)
        assert repeated.status_code == 201, repeated.text
        assert repeated.json()["checkpoint_id"] == str(first)

        changed_review = dict(save)
        changed_review["decisions"] = [{
            **decisions[0], "attributes": {"connection": "wired", "keys": 105},
        }]
        conflict = request(client, prefix + "/save", changed_review, owner.csrf_token)
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "checkpoint_conflict"

        loaded = request(client, prefix + "/restore", restore, owner.csrf_token)
        assert loaded.status_code == 200, loaded.text
        recovered = loaded.json()
        assert recovered["checkpoint_id"] == str(first)
        assert recovered["reviewer_user_id"] == str(reviewer)
        assert recovered["published"] is False
        assert {item["line"] for item in recovered["decisions"]} == {
            decisions[0]["line"], decisions[1]["line"],
        }

        different_mapping = dict(restore)
        different_mapping["mapping"] = {**MAPPING, "confirmed_currency": "RUB"}
        unmatched = request(client, prefix + "/restore", different_mapping, owner.csrf_token)
        assert unmatched.status_code == 200
        assert unmatched.json() is None

        bad_line = dict(restore)
        bad_line["source_sha256"] = "f" * 64
        invalid = request(client, prefix + "/restore", bad_line, owner.csrf_token)
        assert invalid.status_code == 409

        outdated = dict(save)
        outdated["idempotency_key"] = "a73-checkpoint-0002"
        stale = request(client, prefix + "/save", outdated, owner.csrf_token)
        assert stale.status_code == 409
        assert stale.json()["code"] == "checkpoint_conflict"

        truncated = {**outdated,
                     "expected_checkpoint_id": str(first),
                     "decisions": [decisions[0]]}
        lost = request(client, prefix + "/save", truncated, owner.csrf_token)
        assert lost.status_code == 409, lost.text
        assert lost.json()["code"] == "checkpoint_dropped_rows"
        unchanged = request(client, prefix + "/restore", restore, owner.csrf_token)
        assert unchanged.json()["checkpoint_id"] == str(first)
        assert len(unchanged.json()["decisions"]) == 2

        revised = dict(outdated)
        revised["expected_checkpoint_id"] = str(first)
        new_version = request(client, prefix + "/save", revised, owner.csrf_token)
        assert new_version.status_code == 201, new_version.text
        second = new_version.json()["checkpoint_id"]
        assert second != str(first)

    # A new authenticated session can resume the same exact source and schema,
    # but only after re-uploading the original file and verifying every SHA.
    with TestClient(factory("https://testserver"), base_url="https://testserver") as editor_client:
        editor_client.cookies.set(SESSION_COOKIE, editor.session_token)
        resumed = request(editor_client, prefix + "/restore", restore, editor.csrf_token)
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["checkpoint_id"] == second
        assert resumed.json()["previous_id"] == str(first)
        assert resumed.json()["reviewer_user_id"] == str(reviewer)
        assert len(resumed.json()["decisions"]) == 2
        assert request(
            editor_client, outsider_url + "/restore", restore, editor.csrf_token,
        ).status_code == 403
