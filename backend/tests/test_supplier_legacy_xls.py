"""Legacy binary .xls staging with explicit provenance restrictions."""
from __future__ import annotations

from pathlib import Path

import pytest

from kpdesk.catalog.supplier_staging import SupplierMapping, stage_supplier_prices

FIXTURE = Path(__file__).resolve().parent / "fixtures/supplier_legacy.xls"


def test_real_binary_xls_preserves_prices_sku_and_unknown_fields() -> None:
    file = FIXTURE.read_bytes()
    result = stage_supplier_prices(
        "supplier.xls", file, SupplierMapping(
            name=1, price=2, sku=0, currency=3, header_row=1, sheet="Prices",
        ),
    )
    assert len(result.rows) == 2
    assert [item.supplier_sku for item in result.rows] == ["00012", "00013"]
    assert [str(item.price_rub) for item in result.rows] == ["125.5", "499.0"]
    assert all(
        "xls_formula_and_macro_provenance_unverified" in item.issues
        for item in result.rows
    )
    assert result.canonical_importable is False
    assert result.model_training_authorized is False


def test_wrong_xls_signature_or_sheet_does_not_become_ready() -> None:
    original = FIXTURE.read_bytes()
    with pytest.raises(ValueError, match="signature"):
        stage_supplier_prices(
            "supplier.xls", b"fake" + original[4:],
            SupplierMapping(name=1, price=2, header_row=1, sheet="Prices"),
        )
    with pytest.raises(ValueError, match="sheet"):
        stage_supplier_prices(
            "supplier.xls", original,
            SupplierMapping(name=1, price=2, header_row=1, sheet="Unknown"),
        )
