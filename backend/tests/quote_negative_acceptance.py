"""A48 negative quote acceptance on a disposable live-schema tenant database.

Runs real HTTP through the PostgreSQL auth/tenant login roles. No mock quotes.
"""
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text


def _request(quantity: int) -> dict[str, object]:
    return {
        "lines": [{
            "source_quote": "Office paper A4 80",
            "description": "Office paper A4 80",
            "category": "paper",
            "quantity": quantity,
            "unit": "пачка",
            "brand": None,
            "paper_format": "A4",
            "paper_gsm": 80,
            "pen_ink_color": None,
            "pen_ink_type": None,
            "attributes": {},
        }],
    }


def _body(catalog_version: str, quantity: int, *, expected: int | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "catalog_version_id": catalog_version,
        "extracted_request": _request(quantity),
        "manager_selections": [{"line_number": 1, "sku": "E2E-PAPER-B"}],
    }
    if expected is not None:
        payload["expected_revision"] = expected
    return payload


def verify_negative_quote_http(
    client: TestClient, other_client: TestClient, owner_engine: Engine,
    organization: uuid.UUID, outsider: uuid.UUID,
    quote_id: str, csrf: str, other_csrf: str,
) -> str:
    """NEG-01/02/04/05: historical prices, stale revisions, isolation, atomicity."""
    client.headers["Origin"] = "https://testserver"
    other_client.headers["Origin"] = "https://testserver"
    base = f"/api/session/organizations/{organization}/quotes"
    path = f"{base}/{quote_id}"
    first_response = client.get(path, params={"revision": 1})
    assert first_response.status_code == 200, first_response.text
    original = first_response.json()
    catalog_v1 = original["catalog_version_id"]
    assert Decimal(original["lines"][0]["unit_price_rub"]) == Decimal("450.00")
    assert original["lines"][0]["sku"] == "E2E-PAPER-B"

    # NEG-04: an authenticated tenant A manager cannot render B's quote as PDF.
    foreign_pdf = client.get(
        f"/api/session/organizations/{outsider}/quotes/{quote_id}/pdf",
    )
    assert foreign_pdf.status_code == 403
    assert not foreign_pdf.headers.get("content-type", "").startswith("application/pdf")

    first_pdf = client.get(f"{path}/pdf", params={"revision": 1})
    assert first_pdf.status_code == 200 and first_pdf.content.startswith(b"%PDF-")

    headers = {"X-CSRF-Token": csrf, "Idempotency-Key": "a48-revision-two"}
    updated = client.post(
        f"{path}/revisions",
        headers=headers, json=_body(catalog_v1, 3, expected=1),
    )
    assert updated.status_code == 201, updated.text
    assert updated.json()["revision"] == 2
    assert Decimal(updated.json()["total_rub"]) == Decimal("1350.00")

    # NEG-02: an independently submitted stale snapshot is rejected.
    assert other_client.get(path).status_code == 200
    stale = other_client.post(
        f"{path}/revisions",
        headers={"X-CSRF-Token": other_csrf, "Idempotency-Key": "a48-stale-update"},
        json=_body(catalog_v1, 4, expected=1),
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "stale_revision"
    assert client.get(path).json()["revision"] == 2

    # NEG-01: replace the published catalog with a different price for
    # the same SKU. The original immutable quote and PDF must be byte-identical.
    updated_csv = (
        "sku;name;category;sale_unit;pieces_per_sale_unit;price_rub;brand;"
        "paper_format;paper_gsm;pen_ink_color;pen_ink_type;attributes_json\n"
        "E2E-PAPER-A;Office paper A4 80;paper;пачка;1;629.90;;A4;80;;;{}\n"
        "E2E-PAPER-B;Office paper A4 80 second;paper;пачка;1;950.00;;A4;80;;;{}\n"
    ).encode()
    replacement = client.post(
        f"/api/session/organizations/{organization}/catalog/import",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "a48-new-price-v2"},
        files={"file": ("a48-price-changed.csv", updated_csv, "text/csv")},
    )
    assert replacement.status_code == 201, replacement.text
    assert replacement.json()["version_id"] != catalog_v1
    assert client.get(path, params={"revision": 1}).json() == original
    new_pdf = client.get(f"{path}/pdf", params={"revision": 1})
    assert new_pdf.status_code == 200
    assert new_pdf.content == first_pdf.content
    old_revision = client.post(
        f"{path}/revisions",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "a48-old-catalog"},
        json=_body(catalog_v1, 5, expected=2),
    )
    assert old_revision.status_code == 409
    assert old_revision.json()["code"] == "stale_catalog"

    # NEG-05: failure after quote_documents INSERT but during immutable revision
    # insertion must rollback the document, idempotency and audit together.
    # PostgreSQL currently inserts one atomic JSONB snapshot, not 50 row inserts.
    with owner_engine.begin() as conn:
        prior = conn.execute(text("""
            SELECT
              (SELECT count(*) FROM public.quote_documents),
              (SELECT count(*) FROM public.quote_revisions),
              (SELECT count(*) FROM public.tenant_quote_write_requests),
              (SELECT count(*) FROM public.tenant_quote_manager_decisions)
        """)).one()
        conn.exec_driver_sql("""
            CREATE FUNCTION public.kpdesk_a48_test_abort_revision()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'a48_forced_revision_insert_failure'; END $$
        """)
        conn.exec_driver_sql("""
            CREATE TRIGGER trg_a48_test_abort_revision
            AFTER INSERT ON public.quote_revisions
            FOR EACH ROW EXECUTE FUNCTION public.kpdesk_a48_test_abort_revision()
        """)
    try:
        failure = client.post(
            base,
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "a48-rollback"},
            json=_body(replacement.json()["version_id"], 2),
        )
        assert failure.status_code == 503, failure.text
        assert failure.json()["code"] == "quote_store_unavailable"
        with owner_engine.connect() as conn:
            after = conn.execute(text("""
                SELECT
                  (SELECT count(*) FROM public.quote_documents),
                  (SELECT count(*) FROM public.quote_revisions),
                  (SELECT count(*) FROM public.tenant_quote_write_requests),
                  (SELECT count(*) FROM public.tenant_quote_manager_decisions)
            """)).one()
            assert after == prior
    finally:
        with owner_engine.begin() as conn:
            conn.exec_driver_sql("""
                DROP TRIGGER trg_a48_test_abort_revision
                ON public.quote_revisions
            """)
            conn.exec_driver_sql("""
                DROP FUNCTION public.kpdesk_a48_test_abort_revision()
            """)
    return str(replacement.json()["version_id"])


def verify_competing_revisions_http(
    owner_client: TestClient,
    editor_client: TestClient,
    owner_engine: Engine,
    organization: uuid.UUID,
    quote_id: str,
    catalog_v2: str,
    owner_csrf: str,
    editor_csrf: str,
    old_catalog_v1: str,
) -> str:
    """A62: two actual tenant sessions race to revise one optimistic snapshot.

    The transaction-level tenant lock must linearize the writes. Exactly one
    manager can commit revision 3, and the rejected request cannot leave a
    partial idempotency key, price change or manager attestation.
    """
    base = f"/api/session/organizations/{organization}/quotes/{quote_id}"
    original = owner_client.get(base, params={"revision": 1})
    previous = owner_client.get(base, params={"revision": 2})
    assert original.status_code == previous.status_code == 200
    before = owner_client.get(base)
    assert before.status_code == 200 and before.json()["revision"] == 2
    assert before.json()["catalog_version_id"] == old_catalog_v1
    historical = {
        number: {
            "snapshot": response.json(),
            "pdf": owner_client.get(base + "/pdf", params={"revision": number}).content,
        }
        for number, response in ((1, original), (2, previous))
    }
    assert all(data["pdf"].startswith(b"%PDF-") for data in historical.values())

    barrier = Barrier(3)

    def submit(actor: str, client: TestClient, csrf: str, quantity: int) -> tuple[str, int, dict[str, object]]:
        barrier.wait(timeout=20)
        response = client.post(
            base + "/revisions",
            headers={
                "X-CSRF-Token": csrf,
                "Idempotency-Key": f"a62-concurrent-{actor}",
            },
            json=_body(catalog_v2, quantity, expected=2),
        )
        return actor, response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(submit, "owner", owner_client, owner_csrf, 4)
        editor = pool.submit(submit, "editor", editor_client, editor_csrf, 5)
        barrier.wait(timeout=20)
        outcomes = [owner.result(timeout=60), editor.result(timeout=60)]

    accepted = [(actor, payload) for actor, status, payload in outcomes if status == 201]
    rejected = [(actor, payload) for actor, status, payload in outcomes if status == 409]
    assert len(accepted) == len(rejected) == 1, outcomes
    winner, accepted_body = accepted[0]
    loser, rejected_body = rejected[0]
    assert loser != winner
    assert rejected_body["code"] == "stale_revision"
    assert accepted_body["revision"] == 3
    assert accepted_body["catalog_version_id"] == catalog_v2
    quantity = 4 if winner == "owner" else 5
    expected_total = Decimal("950.00") * quantity
    assert Decimal(str(accepted_body["total_rub"])) == expected_total

    current = owner_client.get(base)
    assert current.status_code == 200
    assert current.json() == accepted_body
    assert current.json()["source_request"]["lines"][0]["quantity"] == quantity
    assert current.json()["lines"][0]["sku"] == "E2E-PAPER-B"
    assert current.json()["lines"][0]["status"] == "manager_selected"
    assert Decimal(current.json()["lines"][0]["unit_price_rub"]) == Decimal("950.00")
    assert Decimal(current.json()["lines"][0]["line_total_rub"]) == expected_total
    assert current.json()["snapshot_sha256"] != before.json()["snapshot_sha256"]

    pdf = owner_client.get(base + "/pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-")
    assert current.json()["ready_for_draft"] is True
    for number, data in historical.items():
        reread = owner_client.get(base, params={"revision": number})
        assert reread.status_code == 200
        assert reread.json() == data["snapshot"]
        historical_pdf = owner_client.get(base + "/pdf", params={"revision": number})
        assert historical_pdf.status_code == 200
        assert historical_pdf.content == data["pdf"]

    winning_client = owner_client if winner == "owner" else editor_client
    winning_csrf = owner_csrf if winner == "owner" else editor_csrf
    replay = winning_client.post(
        base + "/revisions",
        headers={
            "X-CSRF-Token": winning_csrf,
            "Idempotency-Key": f"a62-concurrent-{winner}",
        },
        json=_body(catalog_v2, quantity, expected=2),
    )
    assert replay.status_code == 201
    assert replay.json() == accepted_body

    stale_catalog = owner_client.post(
        base + "/revisions",
        headers={"X-CSRF-Token": owner_csrf, "Idempotency-Key": "a62-stale-catalog"},
        json=_body(old_catalog_v1, 6, expected=3),
    )
    assert stale_catalog.status_code == 409
    assert stale_catalog.json()["code"] == "stale_catalog"
    assert owner_client.get(base).json() == accepted_body

    with owner_engine.connect() as connection:
        revisions = connection.execute(text("""
            SELECT number,catalog_version_id,total_rub,snapshot_sha256
            FROM public.quote_revisions
            WHERE organization_id=:org AND quote_id=:quote
            ORDER BY number
        """), {"org": organization, "quote": uuid.UUID(quote_id)}).all()
        assert [row.number for row in revisions] == [1, 2, 3]
        assert revisions[0].snapshot_sha256 == historical[1]["snapshot"]["snapshot_sha256"]
        assert revisions[1].snapshot_sha256 == historical[2]["snapshot"]["snapshot_sha256"]
        assert revisions[2].catalog_version_id == uuid.UUID(catalog_v2)
        assert revisions[2].total_rub == expected_total

        keys = connection.execute(text("""
            SELECT key,revision_number
            FROM public.tenant_quote_write_requests
            WHERE organization_id=:org
              AND key LIKE 'a62-%'
            ORDER BY key
        """), {"org": organization}).all()
        assert keys == [(f"a62-concurrent-{winner}", 3)]
        decisions = connection.execute(text("""
            SELECT line_number,sku,approved_by_user_id,catalog_version_id
            FROM public.tenant_quote_manager_decisions
            WHERE organization_id=:org AND quote_id=:quote AND revision=3
        """), {"org": organization, "quote": uuid.UUID(quote_id)}).all()
        assert len(decisions) == 1
        assert decisions[0].line_number == 1
        assert decisions[0].sku == "E2E-PAPER-B"
        assert decisions[0].catalog_version_id == uuid.UUID(catalog_v2)
    return winner


def verify_catalog_quote_race_http(
    publisher: TestClient,
    quote_editor: TestClient,
    owner_engine: Engine,
    organization: uuid.UUID,
    quote_id: str,
    catalog_v2: str,
    publisher_csrf: str,
    editor_csrf: str,
) -> tuple[str, str]:
    """A63: catalog publication vs. quote revision must serialize at one price version.

    Two authenticated transactions compete through the shared tenant advisory
    mutex; results may be either ordering, never a mixed-catalog quote.
    """
    quote_url = f"/api/session/organizations/{organization}/quotes/{quote_id}"
    catalog_url = f"/api/session/organizations/{organization}/catalog"
    baseline_response = publisher.get(quote_url)
    assert baseline_response.status_code == 200
    baseline = baseline_response.json()
    assert baseline["revision"] == 3
    assert baseline["catalog_version_id"] == catalog_v2
    historical = {
        number: (
            publisher.get(quote_url, params={"revision": number}).json(),
            publisher.get(quote_url + "/pdf", params={"revision": number}).content,
        )
        for number in (1, 2, 3)
    }
    assert all(pdf.startswith(b"%PDF-") for _, pdf in historical.values())
    new_price = Decimal("1025.75")
    source = (
        "sku;name;category;sale_unit;pieces_per_sale_unit;price_rub;brand;"
        "paper_format;paper_gsm;pen_ink_color;pen_ink_type;attributes_json\n"
        "E2E-PAPER-A;Office paper A4 80;paper;пачка;1;735.50;;A4;80;;;{}\n"
        "E2E-PAPER-B;Office paper A4 80 second;paper;пачка;1;1025.75;;A4;80;;;{}\n"
    ).encode()
    barrier = Barrier(3)

    def publish() -> tuple[str, int, dict[str, Any]]:
        barrier.wait(timeout=20)
        response = publisher.post(
            catalog_url + "/import",
            headers={
                "X-CSRF-Token": publisher_csrf,
                "Idempotency-Key": "a63-catalog-publish",
            },
            files={"file": ("a63-updated-prices.csv", source, "text/csv")},
        )
        return "publish", response.status_code, response.json()

    def revise() -> tuple[str, int, dict[str, Any]]:
        barrier.wait(timeout=20)
        response = quote_editor.post(
            quote_url + "/revisions",
            headers={
                "X-CSRF-Token": editor_csrf,
                "Idempotency-Key": "a63-quote-revision",
            },
            json=_body(catalog_v2, 6, expected=3),
        )
        return "revise", response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        publication = pool.submit(publish)
        revision = pool.submit(revise)
        barrier.wait(timeout=20)
        results = {
            name: (status, body)
            for name, status, body in (
                publication.result(timeout=60),
                revision.result(timeout=60),
            )
        }
    status, published = results["publish"]
    assert status == 201, results
    catalog_v3 = str(published["version_id"])
    assert catalog_v3 != catalog_v2
    new_active = publisher.get(catalog_url + "/current")
    assert new_active.status_code == 200
    assert new_active.json()["version_id"] == catalog_v3

    quote_status, response_body = results["revise"]
    assert quote_status in (201, 409), results
    current_response = publisher.get(quote_url)
    assert current_response.status_code == 200
    current = current_response.json()
    if quote_status == 201:
        # Quote locked against V2 before V3 was published.
        assert response_body["revision"] == 4
        assert response_body["catalog_version_id"] == catalog_v2
        assert response_body["source_request"]["lines"][0]["quantity"] == 6
        assert Decimal(str(response_body["lines"][0]["unit_price_rub"])) == Decimal("950.00")
        assert Decimal(str(response_body["total_rub"])) == Decimal("5700.00")
        assert current == response_body
        revision_outcome = "quote_first"
    else:
        # Publication locked V3 first. The stale V2 quote cannot commit.
        assert response_body["code"] == "stale_catalog"
        assert current == baseline
        revision_outcome = "publish_first"

    for number, (prior, pdf) in historical.items():
        unchanged = publisher.get(quote_url, params={"revision": number})
        assert unchanged.status_code == 200
        assert unchanged.json() == prior
        pdf_after = publisher.get(quote_url + "/pdf", params={"revision": number})
        assert pdf_after.status_code == 200 and pdf_after.content == pdf

    # Once publication has committed, an old catalog id cannot be used for a
    # *new* revision even when its optimistic revision number is current.
    rejected = quote_editor.post(
        quote_url + "/revisions",
        headers={
            "X-CSRF-Token": editor_csrf,
            "Idempotency-Key": "a63-stale-after-publish",
        },
        json=_body(catalog_v2, 7, expected=current["revision"]),
    )
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "stale_catalog"

    # A fresh and explicitly reviewed operation must be priced exclusively
    # from V3, irrespective of which transaction won the initial race.
    fresh = quote_editor.post(
        quote_url + "/revisions",
        headers={
            "X-CSRF-Token": editor_csrf,
            "Idempotency-Key": "a63-repriced-v3",
        },
        json=_body(catalog_v3, 2, expected=current["revision"]),
    )
    assert fresh.status_code == 201, fresh.text
    newest = fresh.json()
    assert newest["revision"] == current["revision"] + 1
    assert newest["catalog_version_id"] == catalog_v3
    assert newest["source_request"]["lines"][0]["quantity"] == 2
    assert Decimal(str(newest["lines"][0]["unit_price_rub"])) == new_price
    assert Decimal(str(newest["total_rub"])) == new_price * 2
    assert newest["lines"][0]["status"] == "manager_selected"

    with owner_engine.connect() as connection:
        versions = connection.execute(text("""
            SELECT number,catalog_version_id,total_rub,snapshot_sha256
            FROM public.quote_revisions
            WHERE organization_id=:org AND quote_id=:quote
            ORDER BY number
        """), {"org": organization, "quote": uuid.UUID(quote_id)}).all()
        expected_revisions = list(range(1, newest["revision"] + 1))
        assert [row.number for row in versions] == expected_revisions
        assert versions[-1].catalog_version_id == uuid.UUID(catalog_v3)
        assert versions[-1].total_rub == new_price * 2
        for number, (old, _) in historical.items():
            assert versions[number - 1].snapshot_sha256 == old["snapshot_sha256"]
        if revision_outcome == "quote_first":
            assert versions[3].catalog_version_id == uuid.UUID(catalog_v2)
            assert versions[3].total_rub == Decimal("5700.00")
        a63_keys = connection.execute(text("""
            SELECT key,revision_number
            FROM public.tenant_quote_write_requests
            WHERE organization_id=:org AND key LIKE 'a63-%'
            ORDER BY key
        """), {"org": organization}).all()
        expected_keys = [("a63-repriced-v3", newest["revision"])]
        if revision_outcome == "quote_first":
            expected_keys.insert(0, ("a63-quote-revision", 4))
        assert a63_keys == sorted(expected_keys)
        decisions = connection.execute(text("""
            SELECT revision,line_number,sku,catalog_version_id
            FROM public.tenant_quote_manager_decisions
            WHERE organization_id=:org AND quote_id=:quote AND revision>3
            ORDER BY revision,line_number
        """), {"org": organization, "quote": uuid.UUID(quote_id)}).all()
        assert len(decisions) == (2 if revision_outcome == "quote_first" else 1)
        assert all(item.sku == "E2E-PAPER-B" for item in decisions)
        assert decisions[-1].catalog_version_id == uuid.UUID(catalog_v3)
    return revision_outcome, catalog_v3
