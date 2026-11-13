"""Full HTTP -> service -> PostgreSQL flow against fresh marked clones only.

No TCP server is started, and the application's runtime credentials are never
substituted with the database-owner credentials. They are used only by pytest.
"""

import os
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from postgres_clone_connection import LEGACY_TEST_OPT_IN, legacy_test_database
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from kpdesk.api.persisted_quote_routes import get_owner_secret, get_quote_store
from kpdesk.api.routes import PostgresCatalogRepository, get_catalog_repository
from kpdesk.extraction.schema import ExtractedRequest
from kpdesk.main import create_app
from kpdesk.quotes.storage import (
    StoredQuote,
    create_quote_draft,
    get_quote_draft,
    revise_quote_draft,
)


@pytest.fixture
def test_engine() -> Iterator[Engine]:
    with legacy_test_database() as engine:
        yield engine


@pytest.fixture
def client(test_engine: Engine) -> Iterator[TestClient]:
    class TestDatabaseRepository:
        """Each sync call owns one Session; no cross-thread Session sharing."""

        def publish(self, source: object, idempotency_key: str, request_hash: str) -> object:
            from kpdesk.catalog.schema import CatalogImportResult
            assert isinstance(source, CatalogImportResult)
            with Session(test_engine) as session:
                return PostgresCatalogRepository(session).publish(
                    source, idempotency_key, request_hash,
                )

        def current(self) -> object:
            with Session(test_engine) as session:
                return PostgresCatalogRepository(session).current()

        def items(self, version_id: uuid.UUID) -> object:
            with Session(test_engine) as session:
                return PostgresCatalogRepository(session).items(version_id)

    class TestQuoteRepository:
        """Test-only adapter bound exclusively to a marked disposable clone."""

        def create(
            self, catalog_id: uuid.UUID, extracted: ExtractedRequest, key: str,
        ) -> StoredQuote:
            with Session(test_engine) as session:
                return create_quote_draft(session, catalog_id, extracted, key)

        def revise(
            self, quote_id: uuid.UUID, expected_revision: int,
            catalog_id: uuid.UUID, extracted: ExtractedRequest, key: str,
        ) -> StoredQuote:
            with Session(test_engine) as session:
                return revise_quote_draft(
                    session, quote_id, expected_revision, catalog_id, extracted, key,
                )

        def get(self, quote_id: uuid.UUID, revision: int | None) -> StoredQuote:
            with Session(test_engine) as session:
                return get_quote_draft(session, quote_id, revision)

    repository = TestDatabaseRepository()
    app = create_app(testing=True)
    app.dependency_overrides[get_catalog_repository] = lambda: repository
    app.dependency_overrides[get_quote_store] = lambda: TestQuoteRepository()
    app.dependency_overrides[get_owner_secret] = lambda: "test-owner-token-" + "x" * 44
    with TestClient(app) as c:
        yield c


def test_real_postgres_http_import_read_repeat_and_invalid_catalog(client: TestClient) -> None:
    header = ("sku;name;category;sale_unit;pieces_per_sale_unit;price_rub;"
              "brand;paper_format;paper_gsm;pen_ink_color;pen_ink_type\n")
    first_line = "000009;Бумага А4 80;paper;пачка;1;450.00;Brand;A4;80;;\n"
    file_data = (header + first_line).encode()
    headers = {
        "Origin": "http://localhost:5173",
        "Idempotency-Key": "real-pg-test",
    }
    assert client.get("/api/health/ready").json() == {"status": "ready"}
    first = client.post(
        "/api/catalog/import",
        headers=headers,
        files={"file": ("catalog.csv", file_data, "text/csv")},
    )
    assert first.status_code == 201, first.text
    catalog_id = first.json()["id"]
    assert client.get("/api/catalog/current").json()["id"] == catalog_id
    assert client.get("/api/catalog/items").json()["items"][0]["price_rub"] == "450.00"
    again = client.post(
        "/api/catalog/import",
        headers=headers,
        files={"file": ("catalog.csv", file_data, "text/csv")},
    )
    assert again.status_code == 201
    assert again.json()["id"] == catalog_id

    bad = client.post(
        "/api/catalog/import",
        headers={**headers, "Idempotency-Key": "invalid-pg"},
        files={"file": ("catalog.csv", file_data.replace(b"450.00", b"-1.00"), "text/csv")},
    )
    assert bad.status_code == 422
    assert client.get("/api/catalog/current").json()["id"] == catalog_id
    assert client.get("/api/catalog/items").json()["items"][0]["sku"] == "000009"


def test_v2_postgres_http_extracted_match_and_quote_calculation(client: TestClient) -> None:
    """One real PostgreSQL-backed catalog snapshot through match and RUB calculation."""
    import csv
    import io
    import json

    from kpdesk.catalog.schema import COLUMNS_V2

    stream = io.StringIO(newline="")
    csv.writer(stream, delimiter=";", lineterminator="\n").writerows([
        list(COLUMNS_V2),
        ["M-001", "Мышь беспроводная 1600 DPI", "mouse", "штука", 1,
         "496.51", "SONNEN", "", "", "", "",
         json.dumps({"connection": "wireless", "dpi": 1600})],
        ["M-002", "Мышь проводная 1600 DPI", "mouse", "штука", 1,
         "340.00", "SONNEN", "", "", "", "",
         json.dumps({"connection": "wired", "dpi": 1600})],
    ])
    headers = {"Origin": "http://localhost:5173"}
    imported = client.post(
        "/api/catalog/import",
        headers={**headers, "Idempotency-Key": "full-v2-integration"},
        files={"file": ("mouse.csv", stream.getvalue().encode(), "text/csv")},
    )
    assert imported.status_code == 201, imported.text

    extracted = {
        "source_quote": "две беспроводные мыши 1600 dpi",
        "description": "Беспроводные мыши 1600 dpi",
        "category": "mouse",
        "quantity": 2,
        "unit": "штука",
        "brand": None,
        "paper_format": None,
        "paper_gsm": None,
        "pen_ink_color": None,
        "pen_ink_type": None,
        "attributes": {"connection": "wireless", "dpi": 1600},
    }
    preview = client.post(
        "/api/matches/preview-extracted",
        headers=headers,
        json={"extracted_line": extracted},
    )
    assert preview.status_code == 200, preview.text
    assert [item["sku"] for item in preview.json()["candidates"]] == ["M-001"]
    assert preview.json()["catalog_version_id"] == imported.json()["id"]

    quote = client.post(
        "/api/quotes/calculate",
        headers=headers,
        json={
            "catalog_version_id": imported.json()["id"],
            "lines": [{
                "sku": "M-001",
                "requested_item": {
                    "description": extracted["description"],
                    "category": "mouse", "quantity": 2, "unit": "штука",
                    "attributes": {"connection": "wireless", "dpi": 1600},
                },
            }],
        },
    )
    assert quote.status_code == 200, quote.text
    assert quote.json()["total_rub"] == "993.02"
    assert quote.json()["ready"] is True
    assert quote.json()["requires_manager_confirmation"] is True



def test_postgres_http_saved_quote_revisions_and_immutable_pdf(client: TestClient) -> None:
    import csv
    import io
    import json
    from io import BytesIO

    from pypdf import PdfReader

    from kpdesk.catalog.schema import COLUMNS_V2

    stream = io.StringIO(newline="")
    csv.writer(stream, delimiter=";", lineterminator="\n").writerows([
        list(COLUMNS_V2),
        ["PENCIL-1", "Карандаш графитовый HB", "pencil", "штука", 1, "19.90",
         "Test", "", "", "", "",
         json.dumps({"pencil_type": "graphite", "lead_grade": "HB"})],
    ])
    origin = {"Origin": "http://localhost:5173"}
    imported = client.post(
        "/api/catalog/import",
        headers={**origin, "Idempotency-Key": "quote-catalog"},
        files={"file": ("pencils.csv", stream.getvalue().encode(), "text/csv")},
    )
    assert imported.status_code == 201, imported.text
    catalog_id = imported.json()["id"]

    extracted = {
        "lines": [{
            "source_quote": "Карандаш графитовый HB",
            "description": "Карандаш графитовый HB",
            "category": "pencil", "quantity": 2, "unit": "штука",
            "brand": None, "paper_format": None, "paper_gsm": None,
            "pen_ink_color": None, "pen_ink_type": None,
            "attributes": {"pencil_type": "graphite", "lead_grade": "HB"},
        }],
    }
    auth = {"X-KPDesk-Token": "test-owner-token-" + "x" * 44}
    payload = {"catalog_version_id": catalog_id, "extracted_request": extracted}
    without_owner = client.post(
        "/api/quotes/drafts",
        json=payload,
        headers={**origin, "Idempotency-Key": "private-1"},
    )
    assert without_owner.status_code == 401

    headers = {**origin, **auth, "Idempotency-Key": "private-1"}
    created = client.post("/api/quotes/drafts", json=payload, headers=headers)
    assert created.status_code == 201, created.text
    first = created.json()
    assert first["revision"] == 1
    assert first["total_rub"] == "39.80"
    assert first["ready_for_draft"] is True
    quote_id = first["quote_id"]
    assert client.post("/api/quotes/drafts", json=payload, headers=headers).json() == first
    assert client.post(
        "/api/quotes/drafts",
        json={**payload, "extracted_request": {
            "lines": [{**extracted["lines"][0], "quantity": 3}],
        }},
        headers=headers,
    ).status_code == 409

    first_pdf = client.get(
        f"/api/quotes/drafts/{quote_id}/pdf?revision=1", headers=auth,
    )
    assert first_pdf.status_code == 200, first_pdf.text
    assert "Карандаш графитовый" in PdfReader(BytesIO(first_pdf.content)).pages[0].extract_text()

    newer = {**payload, "expected_revision": 1, "extracted_request": {
        "lines": [{**extracted["lines"][0], "quantity": 3}],
    }}
    second = client.post(
        f"/api/quotes/drafts/{quote_id}/revisions",
        json=newer, headers={**origin, **auth, "Idempotency-Key": "private-2"},
    )
    assert second.status_code == 201, second.text
    assert second.json()["revision"] == 2
    assert second.json()["total_rub"] == "59.70"
    assert client.get(
        f"/api/quotes/drafts/{quote_id}", headers=auth,
    ).json()["revision"] == 2
    assert client.get(
        f"/api/quotes/drafts/{quote_id}?revision=1", headers=auth,
    ).json() == first
    assert client.get(
        f"/api/quotes/drafts/{quote_id}/pdf?revision=1", headers=auth,
    ).content == first_pdf.content
    assert client.post(
        f"/api/quotes/drafts/{quote_id}/revisions",
        json={**newer, "expected_revision": 1},
        headers={**origin, **auth, "Idempotency-Key": "private-3"},
    ).status_code == 409

def test_legacy_http_rejects_anonymous_ai_then_saves_explicit_request_and_pdf(client: TestClient) -> None:
    """Real HTTP→Postgres→matching→locked revision→PDF from an explicit request.

    All database rows live exclusively in a fresh marked disposable clone.
    Anonymous extraction stays closed; this is not authenticated AI acceptance.
    """
    import csv
    import io
    import json
    from collections.abc import AsyncIterator
    from io import BytesIO
    from typing import cast

    import httpx
    from fastapi import FastAPI
    from pypdf import PdfReader

    from kpdesk.api.routes import get_groq_client, get_groq_settings
    from kpdesk.catalog.schema import COLUMNS_V2
    from kpdesk.extraction.groq import GroqSettings

    rows = io.StringIO(newline="")
    csv.writer(rows, delimiter=";", lineterminator="\n").writerows([
        list(COLUMNS_V2),
        ["PENCIL-HB-TEST", "Карандаш графитовый HB", "pencil", "штука", 1,
         "19.90", "", "", "", "", "",
         json.dumps({"pencil_type": "graphite", "lead_grade": "HB"})],
    ])
    origin = {"Origin": "http://localhost:5173"}
    imported = client.post(
        "/api/catalog/import",
        headers={**origin, "Idempotency-Key": "vertical-catalog-v1"},
        files={"file": ("isolated-catalog.csv", rows.getvalue().encode("utf-8"), "text/csv")},
    )
    assert imported.status_code == 201, imported.text
    catalog_id = imported.json()["id"]
    assert client.get("/api/catalog/current").json()["id"] == catalog_id
    assert client.get("/api/catalog/items").json()["items"][0]["sku"] == "PENCIL-HB-TEST"

    source_text = "Нужно 2 карандаша графитовых HB и неизвестный товар."
    line = {
        "source_quote": "2 карандаша графитовых HB",
        "description": "Карандаш графитовый HB",
        "category": "pencil",
        "quantity": 2, "unit": "штука", "brand": None,
        "paper_format": None, "paper_gsm": None,
        "pen_ink_color": None, "pen_ink_type": None,
        "attributes": {"pencil_type": "graphite", "lead_grade": "HB"},
    }
    unresolved_line = {
        **line, "source_quote": "неизвестный товар",
        "description": "Неизвестный товар", "category": None,
        "quantity": None, "unit": None, "attributes": {},
    }
    provider_calls = 0

    def fake_groq(request: httpx.Request) -> httpx.Response:
        nonlocal provider_calls
        provider_calls += 1
        assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer synthetic-test-key"
        assert json.loads(request.content)["messages"][1]["content"] == source_text
        return httpx.Response(200, json={
            "model": "nvidia/nemotron-3-ultra-550b-a55b:free", "choices": [{
                "finish_reason": "stop",
                "message": {"content": json.dumps(
                    {"lines": [line, unresolved_line]}, ensure_ascii=False,
                )},
            }],
        })

    async def fake_groq_client() -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake_groq)) as mock:
            yield mock

    app = cast(FastAPI, client.app)
    app.dependency_overrides[get_groq_settings] = lambda: GroqSettings(test_project_approved=True, 
        api_key="synthetic-test-key", model="nvidia/nemotron-3-ultra-550b-a55b:free",
    )
    app.dependency_overrides[get_groq_client] = fake_groq_client
    extraction = client.post(
        "/api/requests/extract", headers=origin, json={"text": source_text},
    )
    assert extraction.status_code == 503, extraction.text
    assert extraction.json()["code"] == "authenticated_provider_required"
    assert provider_calls == 0
    # The legacy HTTP flow accepts an explicitly supplied, schema-validated
    # synthetic request. Authenticated extraction is tested by tenant scenarios.
    extracted = ExtractedRequest.model_validate({"lines": [line, unresolved_line]}).model_dump(mode="json")
    assert len(extracted["lines"]) == 2
    assert extracted["lines"][0]["quantity"] == 2
    assert extracted["lines"][1]["category"] is None

    preview = client.post(
        "/api/matches/preview-extracted",
        headers=origin, json={"extracted_line": extracted["lines"][0]},
    )
    assert preview.status_code == 200, preview.text
    assert [x["sku"] for x in preview.json()["candidates"]] == ["PENCIL-HB-TEST"]

    draft_preview = client.post(
        "/api/quotes/auto-draft", headers=origin,
        json={"catalog_version_id": catalog_id, "extracted_request": extracted},
    )
    assert draft_preview.status_code == 200, draft_preview.text
    assert [x["status"] for x in draft_preview.json()["lines"]] == [
        "auto_selected", "needs_review",
    ]
    assert draft_preview.json()["total_rub"] is None

    auth = {"X-KPDesk-Token": "test-owner-token-" + "x" * 44}
    create_body = {"catalog_version_id": catalog_id, "extracted_request": extracted}
    stored = client.post(
        "/api/quotes/drafts", headers={
            **origin, **auth, "Idempotency-Key": "vertical-draft-v1",
        }, json=create_body,
    )
    assert stored.status_code == 201, stored.text
    assert stored.json()["ready_for_draft"] is False
    quote_id = stored.json()["quote_id"]
    blocked_pdf = client.get(
        f"/api/quotes/drafts/{quote_id}/pdf?revision=1", headers=auth,
    )
    assert blocked_pdf.status_code == 409
    assert blocked_pdf.json()["code"] == "incomplete_draft"

    # Human correction only: remove the unsupported line, adjust explicit quantity.
    corrected = {
        "lines": [{**extracted["lines"][0], "quantity": 3}],
    }
    updated = client.post(
        f"/api/quotes/drafts/{quote_id}/revisions",
        headers={**origin, **auth, "Idempotency-Key": "vertical-draft-v2"},
        json={
            "catalog_version_id": catalog_id, "expected_revision": 1,
            "extracted_request": corrected,
        },
    )
    assert updated.status_code == 201, updated.text
    assert updated.json()["revision"] == 2
    assert updated.json()["ready_for_draft"] is True
    assert updated.json()["total_rub"] == "59.70"
    assert updated.json()["approved"] is False

    pdf = client.get(
        f"/api/quotes/drafts/{quote_id}/pdf?revision=2", headers=auth,
    )
    assert pdf.status_code == 200, pdf.text
    assert pdf.content.startswith(b"%PDF")
    assert pdf.headers["cache-control"] == "no-store"
    printable = PdfReader(BytesIO(pdf.content))
    assert "Карандаш графитовый" in printable.pages[0].extract_text()
    assert "59.70" in printable.pages[0].extract_text()
    assert client.get(
        f"/api/quotes/drafts/{quote_id}?revision=1", headers=auth,
    ).json()["ready_for_draft"] is False
    assert client.get(
        f"/api/quotes/drafts/{quote_id}?revision=2", headers=auth,
    ).json()["snapshot_sha256"] == updated.json()["snapshot_sha256"]


# Explicit offline accounting authority; production requires PostgreSQL.
pytestmark = [
    pytest.mark.usefixtures("mock_llm_operation"),
    pytest.mark.skipif(
        os.environ.get(LEGACY_TEST_OPT_IN) != "1",
        reason="Explicit opt-in required for per-test marked disposable PostgreSQL clones",
    ),
]
