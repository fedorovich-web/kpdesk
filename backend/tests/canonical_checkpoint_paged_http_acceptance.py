"""A75 real HTTPS/SQL bounded append and paged recovery on the disposable clone."""
from __future__ import annotations

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

ORIGINAL = (
    "sku;name;price;currency;category;unit;pack\n"
    + "".join(
        f"PG-{i:04d};Office keyboard {i};{1000+i}.00;RUB;keyboards;pcs;1\n"
        for i in range(1, 121)
    )
    + "Section: accessories\n"
).encode("utf-8")
MAPPING = {
    "sku": 0, "name": 1, "price": 2, "currency": 3, "category": 4,
    "sale_unit": 5, "pieces_per_sale_unit": 6, "header_row": 1,
}
STAGED = stage_supplier_prices(
    "paging.csv", ORIGINAL,
    SupplierMapping(sku=0, name=1, price=2, currency=3, category=4,
                    sale_unit=5, pieces_per_sale_unit=6, header_row=1),
)


def _decisions() -> list[dict[str, Any]]:
    return [
        ({
            "action": "include", "line": row.line,
            "source_row_sha256": source_row_sha256(row), "confirmed": True,
            "category": "keyboard", "sale_unit": "штука",
            "pieces_per_sale_unit": 1,
            "attributes": {"connection": "wired", "keys": 104},
        } if row.name else {
            "action": "exclude", "line": row.line,
            "source_row_sha256": source_row_sha256(row), "confirmed": True,
            "reason": "This line is a supplier grouping header, not a product.",
        })
        for row in STAGED.rows
    ]


def verify_paged_review_http(
    factory: Callable[[str], FastAPI], organization: uuid.UUID,
    outsider: uuid.UUID, owner: IssuedSession, editor: IssuedSession,
) -> None:
    prefix = (
        f"/api/session/organizations/{organization}"
        "/suppliers/supplier-a75/canonical/checkpoints"
    )
    foreign = prefix.replace(str(organization), str(outsider))
    all_decisions = _decisions()
    # SQL injection-looking text is supplier-controlled data, never SQL.
    injection = "Header'); DROP TABLE public.auth_sessions; -- quoted"
    all_decisions[-1]['reason'] = injection
    assert len(all_decisions) == 121
    reference = {entry["line"]: entry for entry in all_decisions}

    def request(
        client: TestClient, endpoint: str, review: dict[str, Any], csrf: str,
        *, file: bytes = ORIGINAL,
    ) -> Response:
        return cast(Response, client.post(
            endpoint, headers={
                "Origin": "https://testserver", "X-CSRF-Token": csrf,
            },
            files={"file": ("paging.csv", file, "text/csv")},
            data={"review_json": json.dumps(review)},
        ))

    common: dict[str, Any] = {
        "source_sha256": STAGED.sha256,
        "mapping": MAPPING, "formula_policy": "reject",
        "price_basis": "standard",
    }

    with TestClient(factory("https://testserver"), base_url="https://testserver") as client:
        first_batch = {
            **common, "explicitly_confirmed_price_basis": True,
            "idempotency_key": "a75-chunk-001", "expected_checkpoint_id": None,
            "decisions": all_decisions[:45],
        }
        assert request(
            client, prefix + "/append", first_batch, owner.csrf_token,
        ).status_code == 401
        client.cookies.set(SESSION_COOKIE, owner.session_token)
        assert request(
            client, foreign + "/append", first_batch, owner.csrf_token,
        ).status_code == 403
        assert request(
            client, prefix + "/append", first_batch, "invalid-csrf",
        ).status_code == 401

        altered = request(
            client, prefix + "/append", first_batch, owner.csrf_token,
            file=ORIGINAL.replace(b"PG-0001", b"PG-9999"),
        )
        assert altered.status_code == 409
        assert altered.json()["code"] == "source_changed"
        wrong_row = dict(first_batch)
        wrong_row["decisions"] = [{**all_decisions[0], "source_row_sha256": "0" * 64}]
        result = request(client, prefix + "/append", wrong_row, owner.csrf_token)
        assert result.status_code == 409
        assert result.json()["code"] == "source_row_changed"

        injection_key = request(client, prefix + '/append', {
            **first_batch,
            'idempotency_key': "test'); DROP TABLE public.auth_sessions; --",
        }, owner.csrf_token)
        assert injection_key.status_code == 422, injection_key.text
        assert injection_key.json()['code'] == 'invalid_idempotency_key'
        injected_supplier = request(
            client,
            prefix.replace('supplier-a75', 'supplier-a75%27%3BDROP_TABLE'),
            first_batch, owner.csrf_token,
        )
        assert injected_supplier.status_code in (404, 422)

        initial = request(client, prefix + "/append", first_batch, owner.csrf_token)
        assert initial.status_code == 201, initial.text
        first_id = initial.json()["checkpoint_id"]
        repeated = request(client, prefix + "/append", first_batch, owner.csrf_token)
        assert repeated.status_code == 201
        assert repeated.json()["checkpoint_id"] == first_id

        current = first_id
        for index, group in enumerate((all_decisions[45:90], all_decisions[90:]), 2):
            payload = {
                **first_batch,
                "idempotency_key": f"a75-chunk-00{index}",
                "expected_checkpoint_id": current, "decisions": group,
            }
            result = request(client, prefix + "/append", payload, owner.csrf_token)
            assert result.status_code == 201, result.text
            assert result.json()["checkpoint_id"] != current
            current = result.json()["checkpoint_id"]

        overlap = request(client, prefix + "/append", {
            **first_batch, "idempotency_key": "a75-overlap-004",
            "expected_checkpoint_id": current,
            "decisions": [all_decisions[0]],
        }, owner.csrf_token)
        assert overlap.status_code == 409
        assert overlap.json()["code"] == "checkpoint_existing_rows"

        stale = request(client, prefix + "/append", {
            **first_batch, "idempotency_key": "a75-stale-004",
            "expected_checkpoint_id": first_id,
            "decisions": [all_decisions[0]],
        }, owner.csrf_token)
        assert stale.status_code == 409
        assert stale.json()["code"] == "checkpoint_conflict"

        overlong = request(client, prefix + "/append", {
            **first_batch, "idempotency_key": "a75-overlong-004",
            "expected_checkpoint_id": current,
            "decisions": all_decisions[:51],
        }, owner.csrf_token)
        assert overlong.status_code == 422

        first_page = request(client, prefix + "/restore-page", {
            **common, "offset": 0, "limit": 30,
        }, owner.csrf_token)
        assert first_page.status_code == 200, first_page.text
        assert first_page.headers["cache-control"] == "no-store"
        meta = first_page.json()
        assert meta["checkpoint_id"] == current
        assert meta["total_count"] == 121
        assert len(meta["decisions"]) == 30
        assert meta["next_offset"] == 30
        assert meta["published"] is False

        no_lock = request(client, prefix + "/restore-page", {
            **common, "offset": 30, "limit": 30,
        }, owner.csrf_token)
        assert no_lock.status_code == 422
        assert no_lock.json()["code"] == "checkpoint_version_required"

        recovered: list[dict[str, Any]] = []
        page = meta
        while True:
            recovered.extend(page["decisions"])
            if page["next_offset"] is None:
                break
            response = request(client, prefix + "/restore-page", {
                **common, "offset": page["next_offset"], "limit": 30,
                "expected_checkpoint_id": current,
            }, owner.csrf_token)
            assert response.status_code == 200, response.text
            page = response.json()
            assert page["checkpoint_id"] == current
            assert page["total_count"] == 121
        assert len(recovered) == len(reference) == 121
        assert len({entry["line"] for entry in recovered}) == 121
        for entry in recovered:
            original = reference[entry["line"]]
            assert all(entry.get(field) == value for field, value in original.items())

        # An explicitly acknowledged change is a new immutable checkpoint:
        # restore picks the latest decision for one line, without losing 120 others.
        changed = {**all_decisions[0], "attributes": {
            "connection": "wired", "keys": 105,
        }}
        replacement = request(client, prefix + "/append", {
            **first_batch, "idempotency_key": "a75-replace-004",
            "expected_checkpoint_id": current,
            "decisions": [changed], "replace_existing": True,
        }, owner.csrf_token)
        assert replacement.status_code == 201, replacement.text
        new_head = replacement.json()["checkpoint_id"]
        assert new_head != current

        conflicting_page = request(client, prefix + "/restore-page", {
            **common, "offset": 30, "limit": 30,
            "expected_checkpoint_id": current,
        }, owner.csrf_token)
        assert conflicting_page.status_code == 409
        assert conflicting_page.json()["code"] == "checkpoint_changed"

        newest = request(client, prefix + "/restore-page", {
            **common, "limit": 3,
        }, owner.csrf_token)
        assert newest.status_code == 200, newest.text
        latest = newest.json()
        assert latest["checkpoint_id"] == new_head
        assert latest["total_count"] == 121
        assert all(latest["decisions"][0].get(k) == v for k, v in changed.items())
        assert all(
            latest["decisions"][1].get(k) == v for k, v in all_decisions[1].items()
        )

        # Different mapping/source never inherits approvals from old data.
        different = request(client, prefix + "/restore-page", {
            **common,
            "mapping": {**MAPPING, "confirmed_currency": "RUB"},
        }, owner.csrf_token)
        assert different.status_code == 200
        assert different.json() is None
        assert request(
            client, foreign + "/restore-page", common, owner.csrf_token,
        ).status_code == 403

    with TestClient(factory("https://testserver"), base_url="https://testserver") as client:
        client.cookies.set(SESSION_COOKIE, editor.session_token)
        editor_page = request(client, prefix + "/restore-page", {
            **common, "limit": 2,
        }, editor.csrf_token)
        assert editor_page.status_code == 200, editor_page.text
        assert editor_page.json()["checkpoint_id"] == new_head
        assert editor_page.json()["total_count"] == 121
        assert all(
            editor_page.json()["decisions"][0].get(k) == v
            for k, v in changed.items()
        )
