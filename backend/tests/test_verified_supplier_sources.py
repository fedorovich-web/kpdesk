"""Reviewed supplier-to-catalog identity cannot be asserted by the request alone."""
from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from kpdesk.catalog.verified_supplier_sources import (
    SupplierSourceRef,
    load_reviewed_supplier_source,
)
from kpdesk.matching.schema import CatalogProduct


def _source_setup() -> tuple[Mock, SupplierSourceRef, SimpleNamespace]:
    org = uuid.uuid4()
    ref = SupplierSourceRef("supplier_alpha", "a" * 64, uuid.uuid4())
    catalog = Mock()
    catalog.organization_id = org
    catalog.items.return_value = [
        CatalogProduct(
            sku="SAME", name="Карандаш графитовый HB", category="pencil",
            price_rub=Decimal("15.50"), sale_unit="штука",
            pieces_per_sale_unit=1, brand="PaperCo",
            attributes={"pencil_type": "graphite", "lead_grade": "HB"},
        ),
    ]
    catalog.connection.scalar.return_value = {"approved": "stored profile"}
    catalog.connection.execute.return_value.one_or_none.return_value = SimpleNamespace(
        id=ref.catalog_version_id, source_sha256="f" * 64, rows_count=1,
    )
    reviewed = SimpleNamespace(
        organization_id=org, supplier_id=ref.supplier_id,
        fingerprint=ref.profile_fingerprint,
        approved_source_sha256="f" * 64,
        price_basis="standard", confirmed_currency="RUB",
    )
    return catalog, ref, reviewed


def test_reviewed_source_is_bound_to_tenant_profile_original_digest_and_catalog() -> None:
    catalog, ref, reviewed = _source_setup()
    with patch(
        "kpdesk.catalog.verified_supplier_sources.parse_profile_payload",
        return_value=reviewed,
    ):
        source = load_reviewed_supplier_source(catalog, ref)
    catalog._authorize.assert_called_once_with("read")
    assert source.supplier_id == "supplier_alpha"
    assert source.source_sha256 == "f" * 64
    assert source.catalog_version_id == ref.catalog_version_id
    assert source.products[0].price_rub == Decimal("15.50")


@pytest.mark.parametrize(("difference", "error_code"), [
    ({"approved_source_sha256": "e" * 64}, "supplier_catalog_digest_not_reviewed"),
    ({"supplier_id": "other_supplier"}, "supplier_review_profile_scope_mismatch"),
    ({"price_basis": "vat_included"}, "supplier_price_basis_not_comparable"),
    ({"confirmed_currency": None}, "supplier_price_basis_not_comparable"),
])
def test_untrusted_or_incomparable_supplier_review_refused(
    difference: dict[str, str | None], error_code: str,
) -> None:
    catalog, ref, reviewed = _source_setup()
    for key, value in difference.items():
        setattr(reviewed, key, value)
    if "approved_source_sha256" in difference:
        catalog.connection.scalar.side_effect = [
            {"approved": "stored profile"}, None,
        ]
    with patch(
        "kpdesk.catalog.verified_supplier_sources.parse_profile_payload",
        return_value=reviewed,
    ), pytest.raises(ValueError, match=error_code):
        load_reviewed_supplier_source(catalog, ref)


def test_reviewed_canonical_digest_requires_attested_original_mapping_and_version() -> None:
    from kpdesk.catalog.supplier_staging import SupplierMapping, mapping_digest

    catalog, ref, reviewed = _source_setup()
    reviewed.approved_source_sha256 = "e" * 64
    mapping = SupplierMapping(
        name=0, price=1, header_row=1, confirmed_currency="RUB",
    )
    catalog.connection.scalar.side_effect = [
        {"approved": "stored profile"},
        "public.kpdesk_reviewed_catalog_bridge(uuid,uuid,text,text,text,text,text)",
        True,
    ]
    with (
        patch(
            "kpdesk.catalog.verified_supplier_sources.parse_profile_payload",
            return_value=reviewed,
        ),
        patch(
            "kpdesk.catalog.verified_supplier_sources.tabular_mapping_from_profile",
            return_value=mapping,
        ),
    ):
        result = load_reviewed_supplier_source(catalog, ref)
    assert result.source_sha256 == "f" * 64
    assert catalog.connection.scalar.call_count == 3
    params = catalog.connection.scalar.call_args_list[-1].args[1]
    assert params == {
        "org": catalog.organization_id,
        "version": ref.catalog_version_id,
        "supplier": ref.supplier_id,
        "original": "e" * 64,
        "canonical": "f" * 64,
        "mapping_reject": mapping_digest(mapping, "reject"),
        "mapping_quarantine": mapping_digest(mapping, "quarantine_rows"),
    }


@pytest.mark.parametrize("attestation", [False, None, {"not": "true"}])
def test_mismatched_catalog_without_true_bridge_attestation_rejected(
    attestation: object,
) -> None:
    from kpdesk.catalog.supplier_staging import SupplierMapping

    catalog, ref, reviewed = _source_setup()
    reviewed.approved_source_sha256 = "e" * 64
    catalog.connection.scalar.side_effect = [
        {"approved": "stored profile"},
        "public.kpdesk_reviewed_catalog_bridge(uuid,uuid,text,text,text,text,text)",
        attestation,
    ]
    with (
        patch(
            "kpdesk.catalog.verified_supplier_sources.parse_profile_payload",
            return_value=reviewed,
        ),
        patch(
            "kpdesk.catalog.verified_supplier_sources.tabular_mapping_from_profile",
            return_value=SupplierMapping(
                name=0, price=1, header_row=1, confirmed_currency="RUB",
            ),
        ),
        pytest.raises(ValueError, match="supplier_catalog_digest_not_reviewed"),
    ):
        load_reviewed_supplier_source(catalog, ref)
    catalog.items.assert_not_called()


def test_missing_version_or_profile_cannot_be_promoted_into_source() -> None:
    catalog, ref, reviewed = _source_setup()
    catalog.connection.scalar.return_value = None
    with pytest.raises(ValueError, match="supplier_review_profile_not_found"):
        load_reviewed_supplier_source(catalog, ref)
    catalog.connection.scalar.return_value = {"approved": "profile"}
    catalog.connection.execute.return_value.one_or_none.return_value = None
    with patch(
        "kpdesk.catalog.verified_supplier_sources.parse_profile_payload",
        return_value=reviewed,
    ), pytest.raises(ValueError, match="supplier_catalog_version_not_found"):
        load_reviewed_supplier_source(catalog, ref)
