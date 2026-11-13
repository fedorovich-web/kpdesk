"""Category profiles: deterministic compatibility, safe attributes and v1/v2 imports."""

import csv
import io
import json

import pytest
from pydantic import ValidationError

from kpdesk.catalog.parse import parse_price_list
from kpdesk.catalog.schema import COLUMNS, COLUMNS_V2, ImportLimits
from kpdesk.matching.constraints import check_constraints
from kpdesk.matching.retrieve import retrieve_candidates
from kpdesk.matching.schema import CatalogProduct, RequestedItem


def product(category: str, attributes: dict[str, object], sku: str = "SKU-1") -> CatalogProduct:
    return CatalogProduct.model_validate({
        "sku": sku,
        "name": f"Test {category}",
        "category": category,
        "sale_unit": "штука",
        "pieces_per_sale_unit": 1,
        "price_rub": "150.00",
        "attributes": attributes,
    })


@pytest.mark.parametrize(
    ("category", "attributes"),
    [
        ("folder", {"spine_mm": 70}),
        ("stapler", {"capacity_sheets": 100}),
        ("paperclip", {"size_mm": 28, "pack_count": 100}),
        ("keyboard", {"connection": "wired", "keys": 104}),
        ("mouse", {"connection": "wireless", "dpi": 1600}),
        ("usb_drive", {"capacity_gb": 32, "usb_standard": "3.1"}),
    ],
)
def test_supported_profiles_have_typed_required_attributes(
    category: str, attributes: dict[str, object],
) -> None:
    candidate = product(category, attributes)
    request = RequestedItem.model_validate({
        "description": candidate.name, "category": category, "attributes": attributes,
        "quantity": 1, "unit": "штука",
    })
    assert check_constraints(request, candidate).status == "compatible"


@pytest.mark.parametrize(
    ("category", "attributes", "error_key"),
    [
        ("folder", {}, "spine_mm"),
        ("stapler", {}, "capacity_sheets"),
        ("paperclip", {"size_mm": 28}, "pack_count"),
        ("keyboard", {"connection": "wired"}, "keys"),
        ("mouse", {"dpi": 1600}, "connection"),
        ("usb_drive", {"capacity_gb": 32, "arbitrary": "x"}, "arbitrary"),
    ],
)
def test_missing_or_unrecognized_critical_product_attribute_rejected(
    category: str, attributes: dict[str, object], error_key: str,
) -> None:
    with pytest.raises(ValidationError, match=error_key):
        product(category, attributes)


@pytest.mark.parametrize("value", [True, 2.3, ["wired"], None, {"a": 2}, -5])
def test_malformed_attribute_values_rejected(value: object) -> None:
    with pytest.raises(ValidationError):
        product("keyboard", {"connection": value, "keys": 104})


def test_hard_negative_similar_names_never_override_category_constraints() -> None:
    wanted = RequestedItem(
        description="Клавиатура USB 104 клавиши",
        category="keyboard",
        attributes={"connection": "wired", "keys": 104},
    )
    items = [
        product("keyboard", {"connection": "wireless", "keys": 104}, "K-WIFI"),
        product("keyboard", {"connection": "wired", "keys": 104}, "K-USB"),
        product("mouse", {"connection": "wired", "dpi": 1600}, "M-USB"),
    ]
    result = retrieve_candidates(wanted, items, limit=20)
    assert [c.product.sku for c in result] == ["K-USB"]


def test_unknown_request_critical_attribute_requires_manual_review() -> None:
    wanted = RequestedItem(
        description="Нужна клавиатура", category="keyboard", attributes={"keys": 104},
    )
    result = check_constraints(wanted, product(
        "keyboard", {"connection": "wired", "keys": 104}
    ))
    assert result.status == "needs_review"
    assert "connection_request_unknown" in result.reason_codes


def test_numeric_minimum_capacity_and_exact_storage_are_distinct() -> None:
    stapler = product("stapler", {"capacity_sheets": 200})
    request = RequestedItem(
        description="Степлер не менее 100 листов",
        category="stapler", attributes={"capacity_sheets": 100},
    )
    assert check_constraints(request, stapler).status == "compatible"
    assert check_constraints(
        request.model_copy(update={"attributes": {"capacity_sheets": 250}}), stapler
    ).status == "incompatible"
    drive = product("usb_drive", {"capacity_gb": 64, "usb_standard": "3.1"})
    request_drive = RequestedItem(
        description="Флешка 32 ГБ", category="usb_drive",
        attributes={"capacity_gb": 32, "usb_standard": "3.1"},
    )
    assert check_constraints(request_drive, drive).status == "incompatible"


def _csv(rows: list[list[object]]) -> bytes:
    stream = io.StringIO(newline="")
    csv.writer(stream, delimiter=";", lineterminator="\n").writerows(rows)
    return stream.getvalue().encode("utf-8")


def test_legacy_v1_csv_still_imports_and_new_v2_attributes_are_validated() -> None:
    v1 = _csv([
        list(COLUMNS),
        ["P-1", "Бумага A4 80", "paper", "пачка", 1, "349.90",
         "Office", "A4", 80, "", ""],
    ])
    assert parse_price_list("old.csv", v1, ImportLimits()).publishable

    extra = json.dumps({"connection": "wireless", "dpi": 1600})
    v2 = _csv([
        list(COLUMNS_V2),
        ["M-1", "Мышь wireless 1600 DPI", "mouse", "штука", 1,
         "496.51", "Sonnen", "", "", "", "", extra],
    ])
    result = parse_price_list("new.csv", v2, ImportLimits())
    assert result.publishable, result.errors
    assert result.products[0].attributes == {"connection": "wireless", "dpi": 1600}

    bad = _csv([
        list(COLUMNS_V2),
        ["M-1", "Мышь wireless 1600 DPI", "mouse", "штука", 1,
         "496.51", "Sonnen", "", "", "", "",
         json.dumps({"connection": "wireless", "dpi": True})],
    ])
    failed = parse_price_list("bad.csv", bad, ImportLimits())
    assert not failed.publishable


def test_v2_rejects_undeclared_category_and_unallowed_keys() -> None:
    v2 = _csv([
        list(COLUMNS_V2),
        ["BAD", "Unknown", "spaceship", "штука", 1, "2.00", "", "", "", "", "", "{}"],
    ])
    assert not parse_price_list("bad.csv", v2, ImportLimits()).publishable
