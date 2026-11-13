"""Private persisted quote endpoints require a dedicated local owner token."""
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from kpdesk.api.persisted_quote_routes import get_owner_secret, get_quote_store
from kpdesk.main import create_app
from kpdesk.quotes.storage import LockedQuoteLine, QuoteConflict, QuoteNotFound, StoredQuote

ORIGIN = "http://localhost:5173"
KEY = "x" * 48
HEADERS = {"Origin": ORIGIN, "X-KPDesk-Token": KEY, "Idempotency-Key": "quote-create-1"}


def quote() -> StoredQuote:
    return StoredQuote(
        quote_id=uuid4(), revision=1, catalog_version_id=uuid4(),
        ready_for_draft=True, total_rub=Decimal("39.80"), approved=False,
        snapshot_sha256="a" * 64,
        lines=(LockedQuoteLine(
            line_number=1, description="Карандаш графитовый HB", status="auto_selected",
            sku="PENCIL-1", product_name="Карандаш графитовый HB",
            requested_quantity=2, requested_unit="штука",
            sale_quantity=2, sale_unit="штука",
            unit_price_rub=Decimal("19.90"), line_total_rub=Decimal("39.80"),
            reason_codes=(),
        ),),
    )


EXTRACTED = {
    "lines": [{
        "source_quote": "Карандаш графитовый HB",
        "description": "Карандаш графитовый HB",
        "category": "pencil", "quantity": 2, "unit": "штука",
        "brand": None, "paper_format": None, "paper_gsm": None,
        "pen_ink_color": None, "pen_ink_type": None,
        "attributes": {"pencil_type": "graphite", "lead_grade": "HB"},
    }]
}


def test_private_routes_require_owner_token() -> None:
    class Store:
        def create(self, *args: object) -> StoredQuote:
            raise AssertionError("Should not access DB without auth")
    app = create_app(testing=True)
    app.dependency_overrides[get_quote_store] = Store
    app.dependency_overrides[get_owner_secret] = lambda: KEY
    client = TestClient(app)
    body = {"catalog_version_id": str(uuid4()), "extracted_request": EXTRACTED}
    resp = client.post(
        "/api/quotes/drafts", headers={"Origin": ORIGIN, "Idempotency-Key": "a"},
        json=body,
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "owner_auth_required"
    no_key = client.get(f"/api/quotes/drafts/{uuid4()}")
    assert no_key.status_code == 401
    wrong = client.get(
        f"/api/quotes/drafts/{uuid4()}",
        headers={"X-KPDesk-Token": "not-the-owner-token"},
    )
    assert wrong.status_code == 401


def test_create_and_get_saved_quote_and_render_pdf() -> None:
    saved = quote()
    class Store:
        def create(self, catalog_id: UUID, extracted: object, key: str) -> StoredQuote:
            assert catalog_id == saved.catalog_version_id
            assert key == "quote-create-1"
            return saved
        def get(self, quote_id: UUID, revision: int | None) -> StoredQuote:
            assert quote_id == saved.quote_id
            assert revision is None or revision == 1
            return saved
        def revise(self, *args: object) -> StoredQuote:
            raise AssertionError("Revise not called")

    app = create_app(testing=True)
    app.dependency_overrides[get_quote_store] = lambda: Store()
    app.dependency_overrides[get_owner_secret] = lambda: KEY
    client = TestClient(app)
    response = client.post("/api/quotes/drafts",
        headers=HEADERS, json={
            "catalog_version_id": str(saved.catalog_version_id),
            "extracted_request": EXTRACTED,
        })
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["revision"] == 1
    assert body["total_rub"] == "39.80"
    assert body["approved"] is False
    assert body["lines"][0]["unit_price_rub"] == "19.90"

    received = client.get(
        f"/api/quotes/drafts/{saved.quote_id}?revision=1",
        headers={"X-KPDesk-Token": KEY},
    )
    assert received.status_code == 200
    assert received.json() == body
    pdf = client.get(
        f"/api/quotes/drafts/{saved.quote_id}/pdf?revision=1",
        headers={"X-KPDesk-Token": KEY},
    )
    assert pdf.status_code == 200, pdf.text
    assert pdf.content.startswith(b"%PDF")
    assert pdf.headers["cache-control"] == "no-store"
    assert pdf.headers["content-type"] == "application/pdf"


def test_revisions_require_current_revision_and_reject_replays_with_changed_body() -> None:
    saved = quote()
    calls: list[int] = []
    class Store:
        def revise(
            self, quote_id: UUID, expected_revision: int, catalog_id: UUID,
            extracted: object, key: str,
        ) -> StoredQuote:
            calls.append(expected_revision)
            if expected_revision != 1:
                raise QuoteConflict("stale_revision")
            return saved
    app = create_app(testing=True)
    app.dependency_overrides[get_quote_store] = lambda: Store()
    app.dependency_overrides[get_owner_secret] = lambda: KEY
    client = TestClient(app)
    body = {"catalog_version_id": str(saved.catalog_version_id),
            "extracted_request": EXTRACTED, "expected_revision": 1}
    ok = client.post(f"/api/quotes/drafts/{saved.quote_id}/revisions",
                     json=body,headers=HEADERS)
    assert ok.status_code == 201,ok.text
    body["expected_revision"] = 0
    stale = client.post(f"/api/quotes/drafts/{saved.quote_id}/revisions",
                     json=body,headers=HEADERS)
    assert stale.status_code == 422 or stale.status_code == 409
    assert calls == [1]


def test_pdf_incomplete_responds_conflict_and_missing_quote_is_404() -> None:
    base = quote()
    incomplete = StoredQuote(
        quote_id=base.quote_id, revision=1,
        catalog_version_id=base.catalog_version_id,
        ready_for_draft=False, total_rub=None, snapshot_sha256=base.snapshot_sha256,
        lines=(), approved=False,
    )
    class Store:
        def get(self, quote_id: UUID, revision: int | None) -> StoredQuote:
            if quote_id == base.quote_id:
                return incomplete
            raise QuoteNotFound("quote_not_found")
    app = create_app(testing=True)
    app.dependency_overrides[get_quote_store] = lambda: Store()
    app.dependency_overrides[get_owner_secret] = lambda: KEY
    client=TestClient(app)
    blocked=client.get(f"/api/quotes/drafts/{base.quote_id}/pdf",
                       headers={"X-KPDesk-Token":KEY})
    assert blocked.status_code==409
    assert blocked.json()["code"]=="incomplete_draft"
    missing=client.get(f"/api/quotes/drafts/{uuid4()}",
                       headers={"X-KPDesk-Token":KEY})
    assert missing.status_code==404


def test_absent_local_owner_configuration_fails_closed() -> None:
    app = create_app(testing=True)
    app.dependency_overrides[get_owner_secret] = lambda: None
    client = TestClient(app)
    response = client.get(
        f"/api/quotes/drafts/{uuid4()}",
        headers={"X-KPDesk-Token": "any-token"},
    )
    assert response.status_code == 503
    assert response.json()["code"] == "owner_auth_not_configured"
