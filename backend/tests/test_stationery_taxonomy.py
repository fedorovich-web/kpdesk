"""Wide stationery taxonomy is typed and keeps incompatible products distinct."""
from decimal import Decimal

import pytest
from pydantic import ValidationError

from kpdesk.matching.constraints import check_constraints
from kpdesk.matching.profiles import CATEGORY_ATTRIBUTES, REQUIRED_PRODUCT_ATTRIBUTES
from kpdesk.matching.schema import CatalogProduct, RequestedItem


@pytest.mark.parametrize(
    ("category", "attributes"),
    [
        ("pencil", {"pencil_type": "graphite", "lead_grade": "HB"}),
        ("mechanical_pencil", {"lead_diameter_mm_x10": 5}),
        ("marker", {"ink_color": "black", "marker_type": "permanent"}),
        ("highlighter", {"ink_color": "yellow"}),
        ("notebook", {"format": "A5", "sheets": 96}),
        ("document_folder", {"folder_kind": "zip", "format": "A4"}),
        ("plastic_sleeve", {"format": "A4"}),
        ("adhesive_tape", {"width_mm": 19, "length_m": 10}),
        ("glue", {"glue_type": "stick", "volume_ml": 21}),
        ("staple", {"staple_size": "24/6"}),
        ("photo_paper", {"format": "A4", "gsm": 230, "finish": "glossy"}),
        ("printer", {"print_technology": "laser", "color_mode": "mono", "max_format": "A4"}),
        ("toner_cartridge", {
            "cartridge_code": "CF283A", "compatible_models": ["HP LaserJet Pro M125"],
            "color": "black", "oem_status": "original",
        }),
    ],
)
def test_accepts_product_profiles(category: str, attributes: dict[str, object]) -> None:
    product = CatalogProduct.model_validate({
        "sku": "TEST", "name": "Тестовый товар", "category": category,
        "sale_unit": "штука", "pieces_per_sale_unit": 1,
        "price_rub": "100.00", "attributes": attributes,
    })
    assert product.category == category


def test_hard_negative_marker_and_document_folder_type() -> None:
    marker = CatalogProduct(
        sku="MARKER-1", name="Маркер чёрный", category="marker",
        sale_unit="штука", pieces_per_sale_unit=1, price_rub=Decimal("140.00"),
        attributes={"ink_color": "black", "marker_type": "permanent"},
    )
    request = RequestedItem(
        description="Маркер черный на водной основе", category="marker",
        quantity=3, unit="штука",
        attributes={"ink_color": "black", "marker_type": "water"},
    )
    assert check_constraints(request, marker).status == "incompatible"


def test_unknown_category_rejected_and_other_stationery_reviewable() -> None:
    assert "stationery_other" in CATEGORY_ATTRIBUTES
    assert "product_kind" in REQUIRED_PRODUCT_ATTRIBUTES["stationery_other"]
    with pytest.raises(ValidationError):
        CatalogProduct.model_validate({
            "sku": "BAD", "name": "Bad", "category": "made_up",
            "sale_unit": "штука", "pieces_per_sale_unit": 1, "price_rub": "100.00",
        })
    with pytest.raises(ValidationError):
        CatalogProduct.model_validate({
            "sku": "UNSAFE", "name": "Bad", "category": "toner_cartridge",
            "sale_unit": "штука", "pieces_per_sale_unit": 1, "price_rub": "100.00",
            "attributes": {
                "cartridge_code": "CF283A",
                "compatible_models": ["HP 1", 42],
                "color": "black", "oem_status": "original",
            },
        })
