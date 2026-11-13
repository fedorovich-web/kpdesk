"""Automatic multi-line draft; manual review only for ambiguous or unsafe lines."""
from decimal import Decimal

from kpdesk.matching.schema import CatalogProduct, RequestedItem
from kpdesk.quotes.auto_draft import build_auto_draft


def product(
    sku: str,
    category: str,
    name: str,
    attributes: dict[str, str | int | list[str]],
    price: str = "100.00",
) -> CatalogProduct:
    return CatalogProduct(
        sku=sku, name=name, category=category,
        sale_unit="штука", pieces_per_sale_unit=1,
        price_rub=Decimal(price), attributes=attributes,
    )


def request(
    category: str,
    name: str,
    attributes: dict[str, str | int | list[str]],
    quantity: int | None = 2,
) -> RequestedItem:
    return RequestedItem(
        description=name, category=category, quantity=quantity,
        unit="штука", attributes=attributes,
    )


def test_auto_selects_two_unambiguous_stationery_types_and_exact_total() -> None:
    items = [
        product("P1", "pencil", "Карандаш чернографитный HB", {
            "pencil_type": "graphite", "lead_grade": "HB",
        }, "19.90"),
        product("D1", "document_folder", "Папка для документов А4 на молнии", {
            "folder_kind": "zip", "format": "A4",
        }, "122.50"),
    ]
    queries = [
        request("pencil", "Карандаш чернографитный HB", {
            "pencil_type": "graphite", "lead_grade": "HB",
        }, quantity=10),
        request("document_folder", "Папка для документов А4 на молнии", {
            "folder_kind": "zip", "format": "A4",
        }, quantity=2),
    ]
    draft = build_auto_draft(queries, items)
    assert draft.ready_for_draft is True
    assert draft.total_rub == Decimal("444.00")
    assert [line.status for line in draft.lines] == ["auto_selected", "auto_selected"]
    assert [line.sku for line in draft.lines] == ["P1", "D1"]
    assert draft.approved is False


def test_ambiguous_sku_is_not_arbitrarily_chosen() -> None:
    catalog = [
        product("M1", "marker", "Маркер перманентный черный", {
            "marker_type": "permanent", "ink_color": "black",
        }),
        product("M2", "marker", "Маркер перманентный черный", {
            "marker_type": "permanent", "ink_color": "black",
        }),
    ]
    draft = build_auto_draft([
        request("marker", "Маркер перманентный черный", {
            "marker_type": "permanent", "ink_color": "black",
        })
    ], catalog)
    assert draft.total_rub is None
    assert draft.lines[0].status == "needs_review"
    assert draft.lines[0].sku is None
    assert "multiple_compatible_skus" in draft.lines[0].reason_codes


def test_wrong_laser_cartridge_printer_can_never_be_auto_chosen() -> None:
    catalog = [
        product("C1", "toner_cartridge", "Картридж CF283A оригинал", {
            "cartridge_code": "CF283A", "color": "black",
            "oem_status": "original", "compatible_models": ["HP LaserJet Pro M125"],
        }),
    ]
    requested = request("toner_cartridge", "Картридж CF283A для HP LaserJet Pro M126", {
        "printer_model": "HP LaserJet Pro M126",
        "cartridge_code": "CF283A",
        "color": "black",
        "oem_status": "original",
    })
    draft = build_auto_draft([requested], catalog)
    assert draft.ready_for_draft is False
    assert draft.lines[0].status == "no_match"
    assert draft.lines[0].sku is None


def test_uncertain_printer_supply_is_review_not_automatic_success() -> None:
    catalog = [
        product("C1", "toner_cartridge", "Картридж CF283A оригинал", {
            "cartridge_code": "CF283A", "color": "black",
            "oem_status": "original", "compatible_models": ["HP LaserJet Pro M125"],
        }),
    ]
    requested = request("toner_cartridge", "Картридж CF283A оригинал", {
        "cartridge_code": "CF283A", "color": "black",
    })
    draft = build_auto_draft([requested], catalog)
    assert draft.lines[0].status == "needs_review"


def test_unknown_quantity_does_not_get_defaulted() -> None:
    cat = [product("N1", "notebook", "Тетрадь А5 96 листов", {
        "format": "A5", "sheets": 96,
    })]
    requested = request("notebook", "Тетрадь А5 96 листов", {
        "format": "A5", "sheets": 96,
    }, quantity=None)
    draft = build_auto_draft([requested], cat)
    assert draft.lines[0].status == "needs_review"
    assert "missing_quantity_or_unit" in draft.lines[0].reason_codes
    assert draft.total_rub is None


def test_partial_quote_does_not_publish_incomplete_total() -> None:
    items = [
        product("M1", "mouse", "Мышь проводная 1600 DPI", {
            "connection": "wired", "dpi": 1600,
        }, "250.00"),
    ]
    queries = [
        request("mouse", "Мышь проводная 1600 DPI", {
            "connection": "wired", "dpi": 1600,
        }, quantity=2),
        request("glue", "Клей-карандаш 21 мл", {"glue_type": "stick"}, quantity=1),
    ]
    draft = build_auto_draft(queries, items)
    assert [line.status for line in draft.lines] == ["auto_selected", "no_match"]
    assert draft.total_rub is None
    assert draft.lines[0].line_total_rub == Decimal("500.00")
    assert draft.approved is False
