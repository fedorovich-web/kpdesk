"""HTTP contract for owner-gated, read-only vendor staging with no database."""
from __future__ import annotations

import io
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from kpdesk.api import supplier_routes
from kpdesk.api.persisted_quote_routes import get_owner_secret
from kpdesk.main import create_app


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(testing=True)
    app.dependency_overrides[get_owner_secret] = lambda: "test-local-owner-" + "x" * 46
    with TestClient(app) as http:
        yield http


def _headers(*, authenticated: bool = True) -> dict[str, str]:
    headers = {"Origin": "http://localhost:5173"}
    if authenticated:
        headers["X-KPDesk-Token"] = "test-local-owner-" + "x" * 46
    return headers


@pytest.mark.parametrize(('path', 'parser', 'code'), [
    ('stage', 'stage_supplier_prices', 'invalid_source'),
    ('inspect', 'inspect_supplier_source', 'invalid_supplier_source'),
    ('auto-stage', 'inspect_supplier_source', 'invalid_supplier_source'),
    ('commerceml-stage', 'stage_commerceml', 'commerceml_review_required'),
])
def test_supplier_parser_errors_hide_source_diagnostics(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
    path: str, parser: str, code: str,
) -> None:
    def reject(*args: object, **kwargs: object) -> None:
        raise ValueError('PRIVATE-SUPPLIER-SENTINEL customer@example.test <xml>raw source</xml>')

    monkeypatch.setattr(supplier_routes, parser, reject)
    response = client.post('/api/catalog/suppliers/' + path, headers=_headers(),
        files={'file': ('synthetic.csv', b'name;price\nSynthetic;12\n', 'text/csv')},
        data={'mapping_json': json.dumps({
            'name': 0, 'price': 1, 'header_row': 1, 'confirmed_currency': 'RUB',
        })})
    assert response.status_code == 422
    assert response.json()['code'] == code
    assert 'PRIVATE-SUPPLIER-SENTINEL' not in response.text
    assert 'customer@example.test' not in response.text
    assert any('а' <= letter.lower() <= 'я' for letter in response.json()['message'])


def test_supplier_upload_requires_actual_owner_auth(client: TestClient) -> None:
    response = client.post(
        "/api/catalog/suppliers/stage", headers=_headers(authenticated=False),
        files={"file": ("test.csv", b"name;price\nMouse;500\n", "text/csv")},
        data={"mapping_json": json.dumps({
            "name": 0, "price": 1, "header_row": 1, "confirmed_currency": "RUB",
        })},
    )
    assert response.status_code == 401
    assert response.json()["code"] == "owner_auth_required"


def test_real_supplier_aqualiga_http_preview_does_not_publish(client: TestClient) -> None:
    path = (Path(__file__).resolve().parents[2]
            / "ml/data/real/research_candidate_prices/aqualiga_sanitary.xlsx")
    payload = path.read_bytes()
    book = load_workbook(io.BytesIO(payload), read_only=True)
    try:
        sheet = book.sheetnames[0]
    finally:
        book.close()
    response = client.post(
        "/api/catalog/suppliers/stage", headers=_headers(),
        files={"file": ("supplier.xlsx", payload, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"mapping_json": json.dumps({
            "name": 0, "sku": 1, "price": 2, "currency": 3,
            "category": 4, "header_row": 1, "sheet": sheet,
        })},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_rows"] == 3894
    assert body["priced_rub_rows"] == 3894
    assert body["unresolved_rows"] == 3894
    assert len(body["sampled_rows"]) == 20
    assert body["sampled_rows"][0]["source_sale_unit"] is None
    assert body["canonical_importable"] is False
    assert body["model_training_authorized"] is False


def test_noncanonical_csv_without_sku_remains_visible(client: TestClient) -> None:
    response = client.post(
        "/api/catalog/suppliers/stage", headers=_headers(),
        files={"file": ("retail.csv", "Название;Цена\nКлавиатура;2 599,00\n".encode(), "text/csv")},
        data={"mapping_json": json.dumps({
            "name": 0, "price": 1, "header_row": 1, "confirmed_currency": "RUB",
        })},
    )
    assert response.status_code == 200, response.text
    assert response.json()["sampled_rows"][0]["price_rub"] == "2599.00"
    assert "missing_supplier_sku" in response.json()["sampled_rows"][0]["issues"]


def test_supplier_mapping_validation_is_fail_closed(client: TestClient) -> None:
    response = client.post(
        "/api/catalog/suppliers/stage", headers=_headers(),
        files={"file": ("p.csv", b"name;price\nX;12\n", "text/csv")},
        data={"mapping_json": json.dumps({
            "name": 0, "price": 0, "header_row": 1,
        })},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_mapping"


def test_unknown_currency_never_becomes_rub_offer(client: TestClient) -> None:
    response = client.post(
        "/api/catalog/suppliers/stage", headers=_headers(),
        files={"file": ("p.csv", b"name;price\nWidget;21.50\n", "text/csv")},
        data={"mapping_json": json.dumps({"name": 0, "price": 1, "header_row": 1})},
    )
    assert response.status_code == 200
    assert response.json()["priced_rub_rows"] == 0
    assert response.json()["sampled_rows"][0]["price_rub"] is None


def test_upload_rejects_non_local_origin_before_parsing(client: TestClient) -> None:
    response = client.post(
        "/api/catalog/suppliers/stage",
        headers={**_headers(), "Origin": "https://external.example"},
        files={"file": ("p.csv", b"name;price\nWidget;21\n", "text/csv")},
        data={"mapping_json": '{"name":0,"price":1,"header_row":1}'},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "invalid_origin"


def test_json_supplier_http_staging_1c_like_export(client: TestClient) -> None:
    original = json.dumps({
        "products": [
            {"Код": "000077", "Наименование": "Картридж", "Стоимость": "1450.50",
             "Валюта": "RUB"},
        ],
    }, ensure_ascii=False).encode()
    response = client.post(
        "/api/catalog/suppliers/stage", headers=_headers(),
        files={"file": ("1c-export.json", original, "application/json")},
        data={"mapping_json": json.dumps({
            "sku": "Код", "name": "Наименование", "price": "Стоимость",
            "currency": "Валюта", "items_key": "products",
        })},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["priced_rub_rows"] == 1
    assert data["sampled_rows"][0]["supplier_sku"] == "000077"
    assert data["sampled_rows"][0]["price_rub"] == "1450.50"
    assert "category_requires_review" in data["sampled_rows"][0]["issues"]
    assert data["canonical_importable"] is False
    assert data["model_training_authorized"] is False


def test_json_inconsistent_mapping_or_nested_object_fails_closed(client: TestClient) -> None:
    response = client.post(
        "/api/catalog/suppliers/stage", headers=_headers(),
        files={"file": ("1c.json", b'[{"name":{"bad":"nested"},"price":12}]', "application/json")},
        data={"mapping_json": json.dumps({"name": "name", "price": "price",
                                           "confirmed_currency": "RUB"})},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_source"


def test_source_inspection_requires_owner_and_does_not_auto_publish(client: TestClient) -> None:
    body = "Код;Название;Цена\n0001;Бумага;140\n".encode()
    denied = client.post(
        "/api/catalog/suppliers/inspect", headers=_headers(authenticated=False),
        files={"file": ("shop.csv", body, "text/csv")},
    )
    assert denied.status_code == 401
    valid = client.post(
        "/api/catalog/suppliers/inspect", headers=_headers(),
        files={"file": ("shop.csv", body, "text/csv")},
    )
    assert valid.status_code == 200, valid.text
    data = valid.json()
    assert data["row_count"] == 1
    assert data["proposed_fields"] == {"sku": 0, "name": 1, "price": 2}
    assert data["stage_mapping"]["name"] == 1
    assert data["stage_mapping"]["confirmed_currency"] is None
    assert data["canonical_importable"] is False
    assert data["model_training_authorized"] is False
    assert data["sample_rows"] == [["0001", "Бумага", "140"]]


def test_inspection_does_not_choose_between_wholesale_and_retail(client: TestClient) -> None:
    response = client.post(
        "/api/catalog/suppliers/inspect", headers=_headers(),
        files={"file": (
            "vendor.csv", "Код;Название;Цена опт;Цена розница\n"
            "01;Мышь;259;399\n".encode(), "text/csv",
        )},
    )
    assert response.status_code == 200
    data = response.json()
    assert "ambiguous_price_columns" in data["issues"]
    assert data["stage_mapping"] is None
    assert data["options"]["price"] == [2, 3]


def test_inspection_json_mapping_stages_only_matching_source_hash(client: TestClient) -> None:
    raw = json.dumps({"products": [
        {"Код": "000011", "Наименование": "Клавиатура",
         "Цена": "1499,00", "Валюта": "RUB"},
    ]}, ensure_ascii=False).encode()
    inspection = client.post(
        "/api/catalog/suppliers/inspect", headers=_headers(),
        files={"file": ("price.json", raw, "application/json")},
    )
    assert inspection.status_code == 200, inspection.text
    proposed = inspection.json()["stage_mapping"]
    assert proposed["name"] == "Наименование"
    assert proposed["price"] == "Цена"
    proposal = {**proposed, "source_sha256": inspection.json()["source_sha256"]}
    staged = client.post(
        "/api/catalog/suppliers/stage", headers=_headers(),
        files={"file": ("price.json", raw, "application/json")},
        data={"mapping_json": json.dumps(proposal, ensure_ascii=False)},
    )
    assert staged.status_code == 200, staged.text
    assert staged.json()["priced_rub_rows"] == 1
    assert staged.json()["sampled_rows"][0]["supplier_sku"] == "000011"
    tampered = client.post(
        "/api/catalog/suppliers/stage", headers=_headers(),
        files={"file": ("price.json", raw.replace(b"1499", b"3499"), "application/json")},
        data={"mapping_json": json.dumps(proposal, ensure_ascii=False)},
    )
    assert tampered.status_code == 409
    assert tampered.json()["code"] == "source_changed"


def test_inspection_real_aqualiga_sheet_selection_is_explicit(client: TestClient) -> None:
    path = (Path(__file__).resolve().parents[2]
            / "ml/data/real/research_candidate_prices/aqualiga_sanitary.xlsx")
    raw = path.read_bytes()
    first = client.post(
        "/api/catalog/suppliers/inspect", headers=_headers(),
        files={"file": ("real.xlsx", raw, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert first.status_code == 200, first.text
    assert "sheet_selection_required" in first.json()["issues"]
    assert len(first.json()["available_sheets"]) == 2
    selected = client.post(
        "/api/catalog/suppliers/inspect", headers=_headers(),
        files={"file": ("real.xlsx", raw, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"sheet": first.json()["available_sheets"][0]},
    )
    assert selected.status_code == 200, selected.text
    assert selected.json()["row_count"] == 3894
    assert selected.json()["stage_mapping"]["currency"] == 3
    assert selected.json()["requires_confirmation"] is True


def test_auto_stage_handles_supplier_csv_without_manual_mapping(client: TestClient) -> None:
    response = client.post(
        "/api/catalog/suppliers/auto-stage", headers=_headers(),
        files={"file": (
            "supplier.csv",
            "Артикул;Наименование;Цена\n000007;Мышь;1 499,00 RUB\n".encode(),
            "text/csv",
        )},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["published"] is False
    assert body["requires_confirmation"] is True
    assert body["inspection"]["proposed_fields"] == {"sku": 0, "name": 1, "price": 2}
    assert body["staging"]["priced_rub_rows"] == 1
    assert body["staging"]["sampled_rows"][0]["supplier_sku"] == "000007"
    assert body["staging"]["sampled_rows"][0]["price_rub"] == "1499.00"
    assert "category_requires_review" in body["staging"]["sampled_rows"][0]["issues"]


def test_auto_stage_ragged_supplier_rows_are_visible_but_not_publishable(
    client: TestClient,
) -> None:
    source = (
        "Код;Наименование;Цена;Валюта;Ед. изм.\n"
        "A-01;Бумага;240;RUB;пачка\n"
        "Раздел: бумага\n"
        "B-02;Ручка;30;RUB\n"
    ).encode()
    response = client.post(
        "/api/catalog/suppliers/auto-stage", headers=_headers(),
        files={"file": ("supplier.csv", source, "text/csv")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["inspection"]["stage_mapping"] is not None
    assert body["published"] is False
    stage = body["staging"]
    assert stage["total_rows"] == 3
    assert stage["priced_rub_rows"] == 2
    assert stage["canonical_importable"] is False
    assert stage["model_training_authorized"] is False
    section = stage["sampled_rows"][1]
    assert section["line"] == 3
    assert section["price_rub"] is None
    assert "missing_mapped_columns" in section["issues"]
    assert "missing_name" in section["issues"]
    last = stage["sampled_rows"][2]
    assert last["price_rub"] == "30"
    assert "missing_mapped_columns" in last["issues"]
    assert "sale_unit_requires_review" in last["issues"]


def test_auto_stage_does_not_guess_between_two_prices(client: TestClient) -> None:
    response = client.post(
        "/api/catalog/suppliers/auto-stage", headers=_headers(),
        files={"file": (
            "supplier.csv",
            "Код;Название;Цена опт;Цена розница\n001;Мышь;300;499\n".encode(),
            "text/csv",
        )},
    )
    assert response.status_code == 200, response.text
    assert response.json()["staging"] is None
    assert "ambiguous_price_columns" in response.json()["inspection"]["issues"]


def test_auto_stage_json_1c_like_export_with_valid_currency(client: TestClient) -> None:
    payload = json.dumps({"products": [
        {"Код": "000008", "Наименование": "Бумага А4",
         "Цена": "450,00", "Валюта": "RUB"},
    ]}, ensure_ascii=False).encode()
    response = client.post(
        "/api/catalog/suppliers/auto-stage", headers=_headers(),
        files={"file": ("1c.json", payload, "application/json")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["staging"]["priced_rub_rows"] == 1
    assert response.json()["staging"]["sampled_rows"][0]["price_rub"] == "450.00"
    assert response.json()["inspection"]["stage_mapping"]["sku"] == "Код"
    assert response.json()["published"] is False


def test_auto_stage_real_vendor_xlsx_requires_sheet_then_parses(client: TestClient) -> None:
    path = (Path(__file__).resolve().parents[2]
            / "ml/data/real/research_candidate_prices/aqualiga_sanitary.xlsx")
    content = path.read_bytes()
    first = client.post(
        "/api/catalog/suppliers/auto-stage", headers=_headers(),
        files={"file": ("supplier.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert first.status_code == 200, first.text
    assert first.json()["staging"] is None
    assert "sheet_selection_required" in first.json()["inspection"]["issues"]
    again = client.post(
        "/api/catalog/suppliers/auto-stage", headers=_headers(),
        files={"file": ("supplier.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"sheet": first.json()["inspection"]["available_sheets"][0]},
    )
    assert again.status_code == 200, again.text
    assert again.json()["staging"]["total_rows"] == 3894
    assert again.json()["staging"]["priced_rub_rows"] == 3894
    assert again.json()["staging"]["unresolved_rows"] == 3894
    assert again.json()["published"] is False


def test_stage_http_exposes_exact_package_review_reasons_and_all_row_counts(
    client: TestClient,
) -> None:
    source = (
        b"sku;name;price;currency;unit;pack\n"
        b"K1;Item 1;100;RUB;pcs;\n"
        b"K2;Item 2;200;RUB;pcs;0\n"
        b"K3;Item 3;300;RUB;pcs;12\n"
    )
    response = client.post(
        "/api/catalog/suppliers/stage", headers=_headers(),
        files={"file": ("packages.csv", source, "text/csv")},
        data={"mapping_json": json.dumps({
            "sku": 0, "name": 1, "price": 2, "currency": 3,
            "sale_unit": 4, "pieces_per_sale_unit": 5, "header_row": 1,
        })},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["pack_size_issue_counts"] == {
        "pack_size_requires_review": 0,
        "missing_pack_size": 1,
        "invalid_pack_size": 1,
    }
    first, second, third = data["sampled_rows"]
    assert len(first["source_row_sha256"]) == 64
    assert first["issues"][-1] == "missing_pack_size"
    assert "пуст" in first["pack_size_review_reason"].lower()
    assert second["issues"][-1] == "invalid_pack_size"
    assert "некоррект" in second["pack_size_review_reason"].lower()
    assert third["source_pack_size"] == 12
    assert third["pack_size_review_reason"] is None
    assert data["canonical_importable"] is False
    assert data["model_training_authorized"] is False
    assert data["offset"] == 0
    assert data["next_offset"] is None

    page = client.post(
        "/api/catalog/suppliers/stage", headers=_headers(),
        files={"file": ("packages.csv", source, "text/csv")},
        data={
            "mapping_json": json.dumps({
                "sku": 0, "name": 1, "price": 2, "currency": 3,
                "sale_unit": 4, "pieces_per_sale_unit": 5, "header_row": 1,
                "source_sha256": data["source_sha256"],
            }),
            "start": "1", "limit": "1",
        },
    )
    assert page.status_code == 200, page.text
    page_data = page.json()
    assert page_data["total_rows"] == 3
    assert page_data["pack_size_issue_counts"] == data["pack_size_issue_counts"]
    assert page_data["offset"] == 1
    assert page_data["next_offset"] == 2
    assert len(page_data["sampled_rows"]) == 1
    assert page_data["sampled_rows"][0]["supplier_sku"] == "K2"
    assert "invalid_pack_size" in page_data["sampled_rows"][0]["issues"]
    assert page_data["canonical_importable"] is False

    last_page = client.post(
        "/api/catalog/suppliers/stage", headers=_headers(),
        files={"file": ("packages.csv", source, "text/csv")},
        data={
            "mapping_json": json.dumps({
                "sku": 0, "name": 1, "price": 2, "currency": 3,
                "sale_unit": 4, "pieces_per_sale_unit": 5, "header_row": 1,
                "source_sha256": data["source_sha256"],
            }),
            "start": "2", "limit": "1",
        },
    )
    assert last_page.status_code == 200, last_page.text
    assert last_page.json()["next_offset"] is None
    assert last_page.json()["sampled_rows"][0]["supplier_sku"] == "K3"

    unmapped = client.post(
        "/api/catalog/suppliers/stage", headers=_headers(),
        files={"file": ("packages.csv", source, "text/csv")},
        data={"mapping_json": json.dumps({
            "sku": 0, "name": 1, "price": 2, "currency": 3,
            "sale_unit": 4, "header_row": 1,
        })},
    )
    assert unmapped.status_code == 200, unmapped.text
    assert unmapped.json()["pack_size_issue_counts"]["pack_size_requires_review"] == 3
    assert "не выбран" in unmapped.json()["sampled_rows"][0]["pack_size_review_reason"]

def test_ragged_pack_and_original_precise_price_are_visible_for_review(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/catalog/suppliers/stage", headers=_headers(),
        files={"file": ("rows.csv", (
            b"sku;name;price;currency;unit;pack\n"
            b"P-01;Paper;12.345;RUB;pcs;1\n"
            b"Section: supplies\n"
            b"P-02;Folders;20.00;RUB;pcs\n"
        ), "text/csv")},
        data={"mapping_json": json.dumps({
            "sku": 0, "name": 1, "price": 2, "currency": 3,
            "sale_unit": 4, "pieces_per_sale_unit": 5, "header_row": 1,
        })},
    )
    assert response.status_code == 200, response.text
    items = response.json()["sampled_rows"]
    assert len(items) == 3
    assert items[0]["price_rub"] is None
    assert items[0]["source_high_precision_price"] == "12.345"
    assert "price_precision_requires_review" in items[0]["issues"]
    assert "missing_pack_size" in items[1]["issues"]
    assert "missing_mapped_columns" in items[1]["issues"]
    assert "missing_pack_size" in items[2]["issues"]
    assert items[2]["price_rub"] == "20.00"
