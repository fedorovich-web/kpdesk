"""A101: immutable supplier-version diff never approves updated prices."""
from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest

from kpdesk.catalog.supplier_catalog_diff import supplier_catalog_diff
from kpdesk.catalog.verified_supplier_sources import SupplierSourceRef
from kpdesk.matching.schema import CatalogProduct
from kpdesk.quotes.supplier_comparison import VerifiedSupplierCatalog


def _item(
    sku: str, price: str, *, pack: int = 1, brand: str = "PaperCo",
) -> CatalogProduct:
    return CatalogProduct(
        sku=sku, name=f"Карандаш {sku}", category="pencil",
        sale_unit="штука", pieces_per_sale_unit=pack,
        price_rub=Decimal(price), brand=brand,
        attributes={"pencil_type": "graphite", "lead_grade": "HB"},
    )


def _ref(supplier: str = "vendor", version: uuid.UUID | None = None) -> SupplierSourceRef:
    return SupplierSourceRef(supplier, "a" * 64, version or uuid.uuid4())


def _verified(
    ref: SupplierSourceRef, products: tuple[CatalogProduct, ...],
) -> VerifiedSupplierCatalog:
    return VerifiedSupplierCatalog(
        supplier_id=ref.supplier_id, supplier_name=ref.supplier_id,
        catalog_version_id=ref.catalog_version_id,
        source_sha256="b" * 64, products=products,
    )


def test_all_change_categories_price_precision_and_comparable_delta() -> None:
    before, after = _ref(), _ref()
    old = (
        _item("01-remove", "10.00"),
        _item("02-price", "100.05"),
        _item("03-unit", "30.00"),
        _item("04-both", "40.00"),
        _item("05-unchanged", "50.00"),
    )
    new = (
        _item("02-price", "103.10"),
        _item("03-unit", "30.00", pack=2),
        _item("04-both", "45.00", brand="OtherBrand"),
        _item("05-unchanged", "50.00"),
        _item("06-add", "60.00"),
    )
    catalog = Mock()
    with patch(
        "kpdesk.catalog.supplier_catalog_diff.load_reviewed_supplier_source",
        side_effect=[_verified(before, old), _verified(after, new)],
    ) as proof:
        result = supplier_catalog_diff(catalog, before, after, limit=2)
    assert proof.call_count == 2
    assert [entry.sku for entry in result.changes] == ["01-remove", "02-price"]
    assert result.counts == {
        "added": 1, "removed": 1, "price_changed": 1,
        "product_changed": 1, "product_and_price_changed": 1,
    }
    assert result.total_changes == 5
    assert result.next_offset == 2 and result.requires_manager_review
    assert result.changes[1].comparable_price_delta_rub == Decimal("3.05")
    assert result.changes[0].after_price_rub is None

    with patch(
        "kpdesk.catalog.supplier_catalog_diff.load_reviewed_supplier_source",
        side_effect=[_verified(before, old), _verified(after, new)],
    ):
        page = supplier_catalog_diff(catalog, before, after, limit=3, offset=2)
    assert [item.kind for item in page.changes] == [
        "product_changed", "product_and_price_changed", "added",
    ]
    assert page.changes[0].changed_fields == ("pieces_per_sale_unit",)
    assert page.changes[0].comparable_price_delta_rub is None
    assert page.changes[1].changed_fields == ("brand",)
    assert page.changes[1].comparable_price_delta_rub is None
    assert page.next_offset is None
    assert page.before_catalog_version_id == before.catalog_version_id
    assert page.after_catalog_version_id == after.catalog_version_id


@pytest.mark.parametrize(("supplier", "reuse_version", "offset", "limit"), [
    ("other", False, 0, 50),
    ("vendor", True, 0, 50),
    ("vendor", False, -1, 50),
    ("vendor", False, 0, 101),
    ("vendor", False, 10001, 20),
])
def test_invalid_selectors_fail_before_source_loading(
    supplier: str, reuse_version: bool, offset: int, limit: int,
) -> None:
    before = _ref()
    after = _ref(supplier, before.catalog_version_id if reuse_version else None)
    with patch(
        "kpdesk.catalog.supplier_catalog_diff.load_reviewed_supplier_source",
    ) as proof, pytest.raises(ValueError):
        supplier_catalog_diff(Mock(), before, after, offset=offset, limit=limit)
    proof.assert_not_called()


def test_verification_failure_does_not_expose_new_prices() -> None:
    before, after = _ref(), _ref()
    with patch(
        "kpdesk.catalog.supplier_catalog_diff.load_reviewed_supplier_source",
        side_effect=[_verified(before, (_item("old", "11"),)),
                     ValueError("supplier_catalog_digest_not_reviewed")],
    ), pytest.raises(ValueError, match="digest_not_reviewed"):
        supplier_catalog_diff(Mock(), before, after)


def test_unchanged_product_does_not_cause_reconfirmation() -> None:
    before, after = _ref(), _ref()
    item = _item("same", "25.00")
    with patch(
        "kpdesk.catalog.supplier_catalog_diff.load_reviewed_supplier_source",
        side_effect=[
            _verified(before, (item,)), _verified(after, (item,)),
        ],
    ):
        result = supplier_catalog_diff(Mock(), before, after)
    assert result.total_changes == 0
    assert result.changes == ()
    assert result.next_offset is None
    assert not any(result.counts.values())
