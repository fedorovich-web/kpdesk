"""Printer supplies must be checked by exact model compatibility, not fuzzy text."""
from decimal import Decimal

from kpdesk.matching.constraints import check_constraints
from kpdesk.matching.schema import CatalogProduct, RequestedItem


def test_exact_device_compatibility_is_required() -> None:
    cartridge = CatalogProduct(
        sku="INK-001", name="Сменный черный картридж",
        category="ink_cartridge", sale_unit="штука", pieces_per_sale_unit=1,
        price_rub=Decimal("970.00"),
        attributes={
            "cartridge_code": "C-901", "color": "black",
            "oem_status": "compatible", "compatible_models": ["Epson L3250", "Epson L3210"],
        },
    )
    request = RequestedItem(
        description="Картридж для Epson L3250 черный",
        category="ink_cartridge", quantity=1, unit="штука",
        attributes={"printer_model": "Epson L3250", "color": "black", "oem_status": "compatible"},
    )
    assert check_constraints(request, cartridge).status == "compatible"
    wrong = request.model_copy(update={"attributes": {"printer_model": "Epson L3260", "color": "black"}})
    assert check_constraints(wrong, cartridge).status == "incompatible"


def test_missing_requested_device_or_cartridge_code_needs_review() -> None:
    cartridge = CatalogProduct(
        sku="TON-001", name="Картридж HP compatible black",
        category="toner_cartridge", sale_unit="штука", pieces_per_sale_unit=1,
        price_rub=Decimal("1500.00"),
        attributes={
            "cartridge_code": "CF283A", "color": "black",
            "oem_status": "compatible", "compatible_models": ["HP LaserJet Pro M125"],
        },
    )
    request = RequestedItem(
        description="Черный картридж HP", category="toner_cartridge",
        quantity=1, unit="штука", attributes={"color": "black"},
    )
    result = check_constraints(request, cartridge)
    assert result.status == "needs_review"
    assert "cartridge_identifier_missing" in result.reason_codes


def test_inkjet_and_laser_supplies_are_distinct_categories() -> None:
    toner = CatalogProduct(
        sku="TON-002", name="Черный лазерный картридж",
        category="toner_cartridge", sale_unit="штука", pieces_per_sale_unit=1,
        price_rub=Decimal("1750.00"),
        attributes={
            "cartridge_code": "CF283A", "color": "black",
            "oem_status": "original", "compatible_models": ["HP LaserJet Pro M125"],
        },
    )
    ink_request = RequestedItem(
        description="Черный струйный картридж CF283A",
        category="ink_cartridge", quantity=1, unit="штука",
        attributes={"cartridge_code": "CF283A"},
    )
    assert check_constraints(ink_request, toner).status == "incompatible"
