"""Security, format and all-or-nothing validation of the catalog import."""

from __future__ import annotations

import csv
import io
from collections.abc import Callable, Sequence
from dataclasses import replace
from decimal import Decimal
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from openpyxl import Workbook

from kpdesk.catalog.parse import parse_price_list
from kpdesk.catalog.schema import COLUMNS, COLUMNS_V2, CatalogImportResult, ImportLimits


def sample_rows() -> list[list[str]]:
    return [
        list(COLUMNS),
        ["000001", "Бумага А4 80 г/м²", "paper", "пачка", "1", "349.90",
         "Paper", "A4", "80", "", ""],
        ["000002", "Ручка чёрная шариковая", "pen", "коробка", "10", "125.00",
         "Office", "", "", "чёрный", "шариковая"],
    ]


def csv_bytes(rows: list[list[str]]) -> bytes:
    s = io.StringIO(newline="")
    csv.writer(s, delimiter=";", lineterminator="\n").writerows(rows)
    return s.getvalue().encode("utf-8-sig")


def xlsx_bytes(rows: Sequence[Sequence[str | int]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Products"
    for row in rows:
        ws.append(list(row))
    result = io.BytesIO()
    wb.save(result)
    wb.close()
    return result.getvalue()


def codes(result: CatalogImportResult) -> set[str]:
    return {e.code for e in result.errors}


def assert_invalid(result: CatalogImportResult) -> None:
    assert result.products == ()
    assert not result.publishable
    assert result.errors


def test_valid_csv_preserves_text_skus_and_money() -> None:
    result = parse_price_list("supplies.csv", csv_bytes(sample_rows()), ImportLimits())
    assert result.publishable
    assert not result.errors
    assert len(result.products) == 2
    assert result.products[0].sku == "000001"
    assert result.products[0].price_rub == Decimal("349.90")


def test_valid_xlsx_keeps_text_skus() -> None:
    result = parse_price_list("supplies.xlsx", xlsx_bytes(sample_rows()), ImportLimits())
    assert result.publishable
    assert [p.sku for p in result.products] == ["000001", "000002"]


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda r: r[0].__setitem__(1, "wrong_header"), "invalid_header"),
        (lambda r: r[0].__setitem__(1, "sku"), "invalid_header"),
        (lambda r: r[2].__setitem__(0, "000001"), "duplicate_sku"),
        (lambda r: r[1].__setitem__(5, "-1.00"), "invalid_price"),
        (lambda r: r[1].__setitem__(5, "NaN"), "invalid_price"),
        (lambda r: r[1].__setitem__(5, "3,50"), "invalid_price"),
        (lambda r: r[1].__setitem__(5, "1.234"), "invalid_price"),
        (lambda r: r[1].__setitem__(3, "килограмм"), "invalid_product"),
        (lambda r: r[1].__setitem__(1, ""), "invalid_product"),
        (lambda r: r[1].__setitem__(8, ""), "invalid_product"),
        (lambda r: r[1].__setitem__(9, "синий"), "invalid_product"),
        (lambda r: r[2].__setitem__(7, "A4"), "invalid_product"),
        (lambda r: r[1].__setitem__(4, "0"), "invalid_product"),
    ],
)
def test_invalid_csv_is_atomic_with_row_errors(
    change: Callable[[list[list[str]]], None], expected: str,
) -> None:
    rows = sample_rows()
    change(rows)
    result = parse_price_list("test.csv", csv_bytes(rows), ImportLimits())
    assert_invalid(result)
    assert expected in codes(result)
    assert any(e.row is not None for e in result.errors)


def test_csv_limit_and_malformed_utf8_or_binary() -> None:
    too_many = parse_price_list(
        "test.csv", csv_bytes(sample_rows()), replace(ImportLimits(), max_rows=1)
    )
    assert_invalid(too_many)
    assert "too_many_rows" in codes(too_many)

    too_long = parse_price_list(
        "test.csv", csv_bytes(sample_rows()),
        replace(ImportLimits(), max_cell_chars=8),
    )
    assert_invalid(too_long)
    assert "cell_too_long" in codes(too_long)

    for payload in (b"\xff\xfe\xfe", b"\x00\x01" + csv_bytes(sample_rows())):
        assert_invalid(parse_price_list("test.csv", payload, ImportLimits()))


def test_invalid_extension_and_actual_format_mismatch() -> None:
    assert_invalid(parse_price_list("x.xlsx", csv_bytes(sample_rows()), ImportLimits()))
    assert_invalid(parse_price_list("x.csv", xlsx_bytes(sample_rows()), ImportLimits()))
    assert_invalid(parse_price_list("../unsafe.xlsm", b"not spreadsheet", ImportLimits()))


def test_xlsx_formula_never_executes_or_imports_cached_value() -> None:
    rows = sample_rows()
    rows[1][1] = "=2+2"
    assert_invalid(parse_price_list("x.xlsx", xlsx_bytes(rows), ImportLimits()))
    assert "formula_forbidden" in codes(parse_price_list(
        "x.xlsx", xlsx_bytes(rows), ImportLimits()
    ))


def test_xlsx_numeric_sku_rejected_to_prevent_zero_loss() -> None:
    rows = sample_rows()
    mixed_rows: list[list[str | int]] = [list(row) for row in rows]
    mixed_rows[1][0] = 1
    result = parse_price_list("x.xlsx", xlsx_bytes(mixed_rows), ImportLimits())
    assert_invalid(result)
    assert "sku_not_text" in codes(result)


def test_xlsx_rejects_external_links_macro_entries_and_entity_definitions() -> None:
    original = xlsx_bytes(sample_rows())
    for path, data in (
        ("xl/externalLinks/externalLink1.xml", b"<externalLink/>"),
        ("xl/vbaProject.bin", b"badmacro"),
        ("xl/worksheets/extra.xml", b'<!DOCTYPE x [<!ENTITY ex "bad">]><x>&ex;</x>'),
    ):
        out = io.BytesIO()
        with ZipFile(io.BytesIO(original)) as source, ZipFile(out, "w", ZIP_DEFLATED) as target:
            for member in source.infolist():
                target.writestr(member, source.read(member))
            target.writestr(path, data)
        result = parse_price_list("x.xlsx", out.getvalue(), ImportLimits())
        assert_invalid(result)


def test_xlsx_rejects_high_compression_ratio_even_if_zip_is_small() -> None:
    original = xlsx_bytes(sample_rows())
    out = io.BytesIO()
    with ZipFile(io.BytesIO(original)) as source, ZipFile(out, "w", ZIP_DEFLATED) as target:
        for member in source.infolist():
            target.writestr(member, source.read(member))
        target.writestr("xl/worksheets/expanded.xml", b"x" * 500_000)
    result = parse_price_list(
        "x.xlsx", out.getvalue(),
        replace(ImportLimits(), max_compression_ratio=20),
    )
    assert_invalid(result)
    assert "archive_limits" in codes(result)


def test_file_size_limit_before_parsing() -> None:
    payload = csv_bytes(sample_rows())
    result = parse_price_list(
        "test.csv", payload, replace(ImportLimits(), max_upload_bytes=len(payload)-1)
    )
    assert_invalid(result)
    assert "file_too_large" in codes(result)


def _attribute_catalog(
    attributes: str, extension: str, pieces_per_sale_unit: int = 1,
) -> tuple[str, bytes]:
    rows = [list(COLUMNS_V2),
            [*sample_rows()[1], '{}'],
            ['M-001', 'Учебный маркер', 'marker', 'штука', str(pieces_per_sale_unit), '49.90', '', '', '', '', '',
             attributes]]
    content = csv_bytes(rows) if extension == 'csv' else xlsx_bytes(rows)
    return 'attributes.' + extension, content


@pytest.mark.parametrize('extension', ['csv', 'xlsx'])
@pytest.mark.parametrize('attributes', [
    '{"ink_color":"black","marker_type":"whiteboard","marker_type":"permanent"}',
    '{"ink_color":"black","marker_type":"permanent","marker_type":"whiteboard"}',
    '{"ink_color":"black","marker_type":"whiteboard","marker_type":"whiteboard"}',
    r'{"ink_color":"black","marker_type":"whiteboard","marker_\u0074ype":"permanent"}',
])
def test_duplicate_attribute_keys_reject_the_entire_catalog(
    extension: str, attributes: str,
) -> None:
    filename, content = _attribute_catalog(attributes, extension)
    result = parse_price_list(filename, content, ImportLimits())
    assert_invalid(result)
    assert any(e.row == 3 and e.code == 'invalid_product' for e in result.errors)


@pytest.mark.parametrize('extension', ['csv', 'xlsx'])
def test_last_column_obeys_the_same_cell_size_limit(extension: str) -> None:
    attributes = '{"ink_color":"black","marker_type":"whiteboard"}' + ' ' * 2000
    filename, content = _attribute_catalog(attributes, extension)
    result = parse_price_list(filename, content, ImportLimits())
    assert_invalid(result)
    assert any(e.row == 3 and e.field == 'attributes_json' and e.code == 'cell_too_long'
               for e in result.errors)


@pytest.mark.parametrize('extension', ['csv', 'xlsx'])
def test_deep_attributes_return_a_row_error_instead_of_crashing(extension: str) -> None:
    attributes = '{"ink_color":' + '[' * 1500 + '"black"' + ']' * 1500 + '}'
    filename, content = _attribute_catalog(attributes, extension)
    result = parse_price_list(filename, content, replace(ImportLimits(), max_cell_chars=5000))
    assert_invalid(result)
    assert any(e.row == 3 and e.code == 'invalid_product' for e in result.errors)


@pytest.mark.parametrize('extension', ['csv', 'xlsx'])
def test_valid_unique_attributes_keep_their_values(extension: str) -> None:
    filename, content = _attribute_catalog(
        '{"ink_color":"black","marker_type":"whiteboard"}', extension,
    )
    result = parse_price_list(filename, content, ImportLimits())
    assert result.publishable
    assert len(result.products) == 2
    assert result.products[1].attributes == {'ink_color': 'black', 'marker_type': 'whiteboard'}


@pytest.mark.parametrize('extension', ['csv', 'xlsx'])
@pytest.mark.parametrize('attributes,pack_size', [
    ('{"ink_color":"black","marker_type":"whiteboard","marker_type":"permanent"}', 1),
    ('{"ink_color":"black","marker_type":"whiteboard"}' + ' ' * 2000, 1),
    ('{"ink_color":"black","marker_type":"whiteboard"}', 12),
], ids=['duplicate-key', 'cell-too-long', 'piece-pack-conflict'])
def test_preflight_and_import_reject_invalid_products_before_publication(
    monkeypatch: pytest.MonkeyPatch, extension: str, attributes: str, pack_size: int,
) -> None:
    from uuid import uuid4

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from kpdesk.api import tenant_session_routes
    from kpdesk.api.errors import ApiError, json_error
    from kpdesk.api.tenant_session_routes import SESSION_COOKIE, get_unreleased_auth_repository
    from kpdesk.db.tenant_access import AuthenticatedUser

    class FakeCatalog:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def _authorize(self, operation: str) -> None:
            assert operation == 'write'

        def unavailable_categories(self, products: tuple[object, ...]) -> tuple[str, ...]:
            pytest.fail('Invalid input must be rejected before the database category check')

        def publish(self, *args: object) -> None:
            pytest.fail('A catalog with invalid attributes must never be published')

    class FakeRepository:
        connection = None

        def authenticate(
            self, token: str, *, operation: str, csrf: str | None = None,
        ) -> AuthenticatedUser:
            assert token == 'attributes-session'
            assert operation == 'write' and csrf == 'attributes-csrf'
            return AuthenticatedUser(uuid4())

    monkeypatch.setattr(tenant_session_routes, 'TenantCatalogRepository', FakeCatalog)
    app = FastAPI()
    app.include_router(tenant_session_routes.unreleased_router)
    app.state.settings = ImportLimits()
    app.add_exception_handler(ApiError, lambda request, error: json_error(
        request, error.status_code, error.code, error.message,
    ))
    app.dependency_overrides[get_unreleased_auth_repository] = FakeRepository
    filename, content = _attribute_catalog(attributes, extension, pack_size)
    prefix = f'/api/session/organizations/{uuid4()}/catalog'
    headers = {'Origin': 'https://localhost:5173', 'X-CSRF-Token': 'attributes-csrf'}
    mime = 'text/csv' if extension == 'csv' else (
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    with TestClient(app, base_url='https://testserver') as client:
        client.cookies.set(SESSION_COOKIE, 'attributes-session')
        preflight = client.post(prefix + '/preflight', headers=headers,
                                files={'file': (filename, content, mime)})
        assert preflight.status_code == 200
        body = preflight.json()
        assert body['publishable'] is False and body['rows_count'] == 0
        assert body['errors'][0]['row'] == 3
        imported = client.post(prefix + '/import', headers={
            **headers, 'Idempotency-Key': 'invalid-attributes',
            'X-Source-SHA256': body['source_sha256'],
        }, files={'file': (filename, content, mime)})
        assert imported.status_code == 422
        assert imported.json()['code'] == 'catalog_validation_failed'


@pytest.mark.parametrize('extension', ['csv', 'xlsx'])
@pytest.mark.parametrize('category,attributes', [
    ('marker', '{"ink_color":"black","marker_type":"whiteboard"}'),
    ('mouse', '{"connection":"wireless","dpi":1600}'),
    ('stationery_other', '{"product_kind":"item"}'),
])
def test_piece_sale_unit_cannot_contain_multiple_pieces(
    extension: str, category: str, attributes: str,
) -> None:
    rows = [list(COLUMNS_V2), [*sample_rows()[1], '{}'],
            ['BAD-PACK', 'Учебный товар', category, 'штука', '12', '125.00',
             '', '', '', '', '', attributes]]
    content = csv_bytes(rows) if extension == 'csv' else xlsx_bytes(rows)
    result = parse_price_list('pieces.' + extension, content, ImportLimits())
    assert_invalid(result)
    assert any(error.row == 3 and error.code == 'invalid_product' for error in result.errors)
