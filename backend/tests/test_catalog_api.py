"""HTTP contract tests: no production DB credentials or live server required."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import cast

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from kpdesk.api.routes import get_catalog_repository
from kpdesk.catalog.schema import CatalogImportResult
from kpdesk.catalog.storage import CatalogImportConflict, PublishedCatalog
from kpdesk.main import create_app
from kpdesk.matching.schema import CatalogProduct

CSV_HEADER = (
    "sku;name;category;sale_unit;pieces_per_sale_unit;price_rub;"
    "brand;paper_format;paper_gsm;pen_ink_color;pen_ink_type\n"
)
CSV_BODY = "000001;Бумага А4 80;paper;пачка;1;349.90;Paper;A4;80;;\n"


@dataclass
class FakeCatalogRepository:
    published: PublishedCatalog | None = None
    products: tuple[CatalogProduct, ...] = ()
    seen: dict[str, tuple[str, PublishedCatalog]] = field(default_factory=dict)

    def publish(
        self,
        source: CatalogImportResult,
        idempotency_key: str,
        request_hash: str,
    ) -> PublishedCatalog:
        old = self.seen.get(idempotency_key)
        if old is not None:
            old_hash, old_published = old
            if old_hash != request_hash:
                raise CatalogImportConflict("Ключ уже использован.")
            return old_published
        self.published = PublishedCatalog(
            id=uuid.uuid4(),
            source_sha256=source.source_sha256,
            rows_count=len(source.products),
        )
        self.seen[idempotency_key] = (request_hash, self.published)
        self.products = source.products
        return self.published

    def current(self) -> PublishedCatalog | None:
        return self.published

    def items(self, version_id: uuid.UUID) -> list[CatalogProduct]:
        if self.published is None or version_id != self.published.id:
            return []
        return list(self.products)


@pytest.fixture
def client() -> TestClient:
    repository = FakeCatalogRepository()
    app = create_app(testing=True)
    app.dependency_overrides[get_catalog_repository] = lambda: repository
    return TestClient(app)


def upload(
    client: TestClient,
    payload: bytes,
    key: str = "upload-once",
    origin: str = "http://127.0.0.1:5173",
) -> Response:
    return cast(Response, client.post(
        "/api/catalog/import",
        files={"file": ("catalog.csv", payload, "text/csv")},
        headers={"Idempotency-Key": key, "Origin": origin},
    ))


def test_liveness_and_unconfigured_capabilities(client: TestClient) -> None:
    assert client.get("/api/health/live").json() == {"status": "ok"}
    capabilities = client.get("/api/capabilities").json()
    assert capabilities["model_trained"] is False
    assert capabilities["llm_available"] is False


def test_successful_upload_and_catalog_readback(client: TestClient) -> None:
    response = upload(client, (CSV_HEADER + CSV_BODY).encode())
    assert response.status_code == 201
    body = response.json()
    assert body["rows_count"] == 1
    assert body["source_sha256"]
    active = client.get("/api/catalog/current")
    assert active.status_code == 200
    assert active.json()["id"] == body["id"]
    items = client.get("/api/catalog/items")
    assert items.status_code == 200
    assert items.json()["items"][0]["sku"] == "000001"
    assert items.json()["items"][0]["price_rub"] == "349.90"


def test_upload_is_idempotent_and_changed_file_conflicts(client: TestClient) -> None:
    payload = (CSV_HEADER + CSV_BODY).encode()
    first = upload(client, payload)
    again = upload(client, payload)
    assert first.status_code == 201
    assert again.status_code == 201
    assert first.json() == again.json()
    different = upload(client, payload.replace(b"000001", b"000002"))
    assert different.status_code == 409
    assert different.json()["code"] == "idempotency_conflict"


@pytest.mark.parametrize(('column', 'private_value'), [
    (2, 'PRIVATE-CATEGORY-SENTINEL'), (3, 'PRIVATE-UNIT-SENTINEL'),
])
def test_invalid_product_error_keeps_row_without_echoing_validator_input(
    client: TestClient, column: int, private_value: str,
) -> None:
    values = CSV_BODY.strip().split(';')
    values[column] = private_value
    response = upload(client, (CSV_HEADER + ';'.join(values) + '\n').encode())
    assert response.status_code == 422
    assert response.json()['code'] == 'invalid_catalog'
    assert response.json()['row'] == 2
    assert private_value not in response.text
    assert 'input_value' not in response.text
    assert 'errors.pydantic.dev' not in response.text
    assert client.get('/api/catalog/current').status_code == 404


def test_invalid_csv_has_row_details_without_publish(client: TestClient) -> None:
    bad = (CSV_HEADER + CSV_BODY.replace("349.90", "-3.00")).encode()
    response = upload(client, bad)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "invalid_catalog"
    assert body["row"] == 2
    assert body["request_id"]
    assert client.get("/api/catalog/current").status_code == 404


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({"Origin": "https://evil.example", "Idempotency-Key": "k"}, 403),
        ({"Host": "evil.example", "Idempotency-Key": "k"}, 400),
    ],
)
def test_host_and_origin_enforcement(
    client: TestClient, headers: dict[str, str], expected: int,
) -> None:
    response = client.post(
        "/api/catalog/import",
        files={"file": ("x.csv", (CSV_HEADER + CSV_BODY).encode(), "text/csv")},
        headers=headers,
    )
    assert response.status_code == expected
    assert response.json()["request_id"]


def test_upload_requires_idempotency_key(client: TestClient) -> None:
    response = client.post(
        "/api/catalog/import",
        files={"file": ("x.csv", (CSV_HEADER + CSV_BODY).encode(), "text/csv")},
        headers={"Origin": "http://127.0.0.1:5173"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "missing_idempotency_key"


def test_body_limit_and_disallowed_extension(client: TestClient) -> None:
    response = upload(client, b"x" * (10 * 1024 * 1024 + 1))
    assert response.status_code == 413
    assert response.json()["code"] == "file_too_large"

    response = client.post(
        "/api/catalog/import",
        files={"file": ("virus.xlsm", b"invalid", "application/octet-stream")},
        headers={"Idempotency-Key": "file-2", "Origin": "http://127.0.0.1:5173"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_catalog"


def test_empty_catalog_is_not_advertised_as_ready(client: TestClient) -> None:
    assert client.get("/api/catalog/current").status_code == 404
    assert client.get("/api/catalog/items").json() == {"items": []}


def test_declared_oversized_request_is_rejected_before_parsing(client: TestClient) -> None:
    response = client.post(
        "/api/catalog/import",
        headers={
            "Origin": "http://localhost:5173",
            "Idempotency-Key": "declared-big",
            "Content-Length": str(20 * 1024 * 1024),
        },
        files={"file": ("catalog.csv", (CSV_HEADER + CSV_BODY).encode(), "text/csv")},
    )
    assert response.status_code == 413
    assert response.json()["code"] == "file_too_large"
    assert client.get("/api/catalog/current").status_code == 404


def test_missing_origin_rejected_on_write(client: TestClient) -> None:
    response = client.post(
        "/api/catalog/import",
        files={"file": ("catalog.csv", (CSV_HEADER + CSV_BODY).encode(), "text/csv")},
        headers={"Idempotency-Key": "no-origin"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "missing_origin"


def test_non_loopback_host_rejected_even_for_health(client: TestClient) -> None:
    response = client.get("/api/health/live", headers={"Host": "localhost.evil"})
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_host"
