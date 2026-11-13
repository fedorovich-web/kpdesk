"""Canonical supplier export requires real source values and explicit reviewer decisions."""
from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import Literal

import pytest

from kpdesk.catalog.supplier_canonical_preview import (
    CanonicalExportRejected,
    CanonicalPreview,
    ReviewedExclusion,
    ReviewedProductDecision,
    preview_canonical_catalog,
    source_row_sha256,
)
from kpdesk.catalog.supplier_staging import (
    SupplierMapping,
    SupplierStaging,
    stage_supplier_prices,
)
from kpdesk.db.tenant_access import AuthenticatedUser

ORG = uuid.UUID("60000000-0000-4000-8000-000000000001")
REVIEWER = AuthenticatedUser(uuid.UUID("60000000-0000-4000-8000-000000000002"))


def _staged() -> SupplierStaging:
    raw = (
        b"sku;name;price;currency;unit;pack\n"
        b"K-001;Office keyboard;1500.90;RUB;pcs;1\n"
        b"M-002;USB mouse;899.00;RUB;pcs;1\n"
    )
    result = stage_supplier_prices(
        "supplier.csv", raw,
        SupplierMapping(sku=0, name=1, price=2, currency=3, sale_unit=4,
                        pieces_per_sale_unit=5, header_row=1),
    )
    return result


def _decisions(staged: SupplierStaging) -> tuple[ReviewedProductDecision, ...]:
    return (
        ReviewedProductDecision(
            line=staged.rows[0].line,
            category="keyboard", sale_unit="штука", pieces_per_sale_unit=1,
            source_row_sha256=source_row_sha256(staged.rows[0]),
            confirmed=True,
            attributes={"connection": "wired", "keys": 104},
        ),
        ReviewedProductDecision(
            line=staged.rows[1].line,
            category="mouse", sale_unit="штука", pieces_per_sale_unit=1,
            source_row_sha256=source_row_sha256(staged.rows[1]),
            confirmed=True,
            attributes={"connection": "wired", "dpi": 1600},
        ),
    )


def _render(
    staged: SupplierStaging,
    decisions: tuple[ReviewedProductDecision | ReviewedExclusion, ...],
    *,
    organization_id: uuid.UUID = ORG,
    reviewer: AuthenticatedUser = REVIEWER,
    source_sha256: str | None = None,
    price_basis: Literal["standard", "retail", "wholesale", "rrp", "vat_included"] = "standard",
    confirmed_price_basis: bool = True,
) -> CanonicalPreview:
    return preview_canonical_catalog(
        staged, decisions, organization_id=organization_id,
        reviewer=reviewer,
        expected_source_sha256=source_sha256 or staged.sha256,
        price_basis=price_basis, explicitly_confirmed_price_basis=confirmed_price_basis,
    )


def test_two_reviewed_products_export_to_exact_importable_kpdesk_catalog() -> None:
    staged = _staged()
    result = _render(staged, _decisions(staged))
    assert result.products_count == 2
    assert result.validation.publishable
    assert [p.sku for p in result.validation.products] == ["K-001", "M-002"]
    assert result.validation.products[0].attributes == {
        "connection": "wired", "keys": 104,
    }
    assert str(result.validation.products[0].price_rub) == "1500.90"
    assert result.csv_content.startswith(
        b"sku;name;category;sale_unit;pieces_per_sale_unit;price_rub;"
    )
    assert hashlib.sha256(result.csv_content).hexdigest() == result.csv_sha256
    assert result.published is False
    assert result.model_training_authorized is False


def test_one_missing_decision_blocks_whole_file_without_partial_success() -> None:
    staged = _staged()
    with pytest.raises(CanonicalExportRejected, match="approval_or_source_mismatch"):
        _render(staged, _decisions(staged)[:1])


@pytest.mark.parametrize("mutation", [
    lambda d: replace(d, confirmed=False),
    lambda d: replace(d, source_row_sha256="a" * 64),
])
def test_unapproved_or_stale_source_row_blocks_export(
    mutation: Callable[[ReviewedProductDecision], ReviewedProductDecision],
) -> None:
    staged = _staged()
    original = _decisions(staged)
    decisions = (mutation(original[0]), original[1])
    with pytest.raises(CanonicalExportRejected, match="unconfirmed_or_changed_source_row"):
        _render(staged, decisions)


def test_duplicate_or_foreign_row_review_rejected() -> None:
    staged = _staged()
    original = _decisions(staged)
    with pytest.raises(CanonicalExportRejected, match="missing_or_unknown_review_decision"):
        _render(staged, (original[0], replace(original[1], line=999)))
    with pytest.raises(CanonicalExportRejected, match="duplicate_review_decision"):
        _render(staged, (original[0], replace(original[1], line=original[0].line)))


def test_no_original_sku_does_not_generate_one() -> None:
    raw = b"name;price\nUnidentified keyboard;700 RUB\n"
    stage = stage_supplier_prices(
        "supplier.csv", raw, SupplierMapping(name=0, price=1, header_row=1),
    )
    decision = ReviewedProductDecision(
        line=stage.rows[0].line, category="keyboard",
        sale_unit="штука", pieces_per_sale_unit=1,
        source_row_sha256=source_row_sha256(stage.rows[0]), confirmed=True,
        attributes={"connection": "wired", "keys": 104},
    )
    with pytest.raises(CanonicalExportRejected, match="missing_original_identity_or_rub_price"):
        _render(stage, (decision,))


def test_unresolved_currency_does_not_become_rub_catalog() -> None:
    stage = stage_supplier_prices(
        "supplier.csv", b"sku;name;price\nK1;Keyboard;500\n",
        SupplierMapping(sku=0, name=1, price=2, header_row=1),
    )
    decision = ReviewedProductDecision(
        line=stage.rows[0].line, category="keyboard",
        sale_unit="штука", pieces_per_sale_unit=1,
        source_row_sha256=source_row_sha256(stage.rows[0]), confirmed=True,
        attributes={"connection": "wired", "keys": 104},
    )
    with pytest.raises(CanonicalExportRejected, match="missing_original_identity_or_rub_price"):
        _render(stage, (decision,))


def test_category_unit_and_required_matching_attributes_are_validated() -> None:
    staged = _staged()
    original = _decisions(staged)
    with pytest.raises(CanonicalExportRejected, match="unsupported_or_unmapped_category"):
        _render(staged, (replace(original[0], category="guess_random"), original[1]))
    with pytest.raises(CanonicalExportRejected, match="sale_unit_or_pack_review_required"):
        _render(staged, (replace(original[0], pieces_per_sale_unit=0), original[1]))
    with pytest.raises(CanonicalExportRejected, match="invalid_product"):
        _render(staged, (replace(original[0], attributes={}), original[1]))


def test_spreadsheet_formula_injection_name_is_rejected() -> None:
    stage = stage_supplier_prices(
        "supplier.csv", b"sku;name;price;unit;pack\nA-01;=HYPERLINK(1);100 RUB;pcs;1\n",
        SupplierMapping(sku=0, name=1, price=2, sale_unit=3,
                        pieces_per_sale_unit=4, header_row=1),
    )
    decision = ReviewedProductDecision(
        line=stage.rows[0].line, category="stationery_other",
        sale_unit="штука", pieces_per_sale_unit=1, confirmed=True,
        source_row_sha256=source_row_sha256(stage.rows[0]),
        attributes={"product_kind": "item"},
    )
    with pytest.raises(CanonicalExportRejected, match="invalid_reviewed_product"):
        _render(stage, (decision,))


def test_original_xls_cached_formulas_always_block_direct_export() -> None:
    from pathlib import Path

    file = Path(__file__).parent / "fixtures/supplier_legacy.xls"
    stage = stage_supplier_prices(
        file.name, file.read_bytes(),
        SupplierMapping(sku=0, name=1, price=2, currency=3,
                        header_row=1, sheet="Prices"),
    )
    decisions = tuple(ReviewedProductDecision(
        line=row.line, category="stationery_other", sale_unit="штука",
        pieces_per_sale_unit=1, confirmed=True,
        source_row_sha256=source_row_sha256(row),
        attributes={"product_kind": "item"},
    ) for row in stage.rows)
    with pytest.raises(CanonicalExportRejected, match="unverified_legacy_xls"):
        _render(stage, decisions)


def test_duplicate_sku_rejected_before_catalog_version_proposal() -> None:
    staged = stage_supplier_prices(
        "source.csv",
        b"sku;name;price;unit;pack\nABC;Item one;100 RUB;pcs;1\nABC;Item two;150 RUB;pcs;1\n",
        SupplierMapping(sku=0, name=1, price=2, sale_unit=3,
                        pieces_per_sale_unit=4, header_row=1),
    )
    decisions = tuple(ReviewedProductDecision(
        line=row.line, category="stationery_other", sale_unit="штука",
        pieces_per_sale_unit=1, confirmed=True,
        source_row_sha256=source_row_sha256(row),
        attributes={"product_kind": "item"},
    ) for row in staged.rows)
    with pytest.raises(CanonicalExportRejected, match="duplicate_original_supplier_sku"):
        _render(staged, decisions)


def test_untrusted_reviewer_or_wrong_source_hash_rejected() -> None:
    staged = _staged()
    decisions = _decisions(staged)
    with pytest.raises(CanonicalExportRejected, match="approval_or_source_mismatch"):
        _render(staged, decisions, reviewer="fake user")  # type: ignore[arg-type]
    with pytest.raises(CanonicalExportRejected, match="approval_or_source_mismatch"):
        _render(staged, decisions, source_sha256="0" * 64)


def test_one_explicitly_excluded_problem_row_creates_valid_partial_catalog() -> None:
    staged = _staged()
    first, second = _decisions(staged)
    exclusion = ReviewedExclusion(
        line=second.line, source_row_sha256=source_row_sha256(staged.rows[1]),
        confirmed=True, reason="Supplier product requires missing compatibility data.",
    )
    result = _render(staged, (first, exclusion))
    assert result.products_count == 1
    assert result.excluded_source_rows == (second.line,)
    assert [p.sku for p in result.validation.products] == ["K-001"]
    assert result.validation.publishable
    assert result.model_training_authorized is False


def test_cannot_silently_exclude_row_or_use_too_short_reason() -> None:
    staged = _staged()
    first, second = _decisions(staged)
    bad = ReviewedExclusion(
        line=second.line, source_row_sha256=source_row_sha256(staged.rows[1]),
        confirmed=True, reason="bad",
    )
    with pytest.raises(CanonicalExportRejected, match="exclusion_reason_required"):
        _render(staged, (first, bad))
    with pytest.raises(CanonicalExportRejected, match="unconfirmed_or_changed_source_row"):
        _render(staged, (first, replace(bad, reason="Supplier unknown SKU", confirmed=False)))


def test_all_excluded_rows_do_not_produce_empty_published_catalog() -> None:
    staged = _staged()
    exclusions = tuple(
        ReviewedExclusion(
            line=row.line, source_row_sha256=source_row_sha256(row),
            confirmed=True, reason="Human reviewer excluded this source entry",
        ) for row in staged.rows
    )
    with pytest.raises(CanonicalExportRejected, match="empty_reviewed_catalog"):
        _render(staged, exclusions)


def test_reviewed_price_tier_must_be_explicitly_confirmed() -> None:
    staged = _staged()
    with pytest.raises(CanonicalExportRejected, match="approval_or_source_mismatch"):
        _render(staged, _decisions(staged), confirmed_price_basis=False)
    result = _render(
        staged, _decisions(staged), price_basis="retail", confirmed_price_basis=True,
    )
    assert result.reviewed_price_basis == "retail"
    assert result.published is False


def test_end_to_end_reviewed_supplier_profile_refresh_to_canonical_without_db() -> None:
    from kpdesk.catalog.supplier_inference import inspect_supplier_source
    from kpdesk.catalog.supplier_profiles import (
        approve_supplier_profile,
        reuse_supplier_profile,
    )

    original = (
        b"sku;name;price;currency;unit;pack\n"
        b"K-001;Office keyboard;1500.90;RUB;pcs;1\n"
        b"M-002;USB mouse;899.00;RUB;pcs;1\n"
    )
    new_offer = original.replace(b"1500.90", b"1699.00")
    inspected_old = inspect_supplier_source("supplier.csv", original)
    reviewed_mapping = approve_supplier_profile(
        inspected_old, organization_id=ORG, supplier_id="office_vendor",
        reviewer=REVIEWER, source_sha256=inspected_old.source_sha256,
        selected_columns={
            "sku": 0, "name": 1, "price": 2, "currency": 3,
            "sale_unit": 4, "pieces_per_sale_unit": 5,
        },
        price_basis="standard", confirmed_currency=None,
        review_evidence="Independent human checked supplier header meanings",
        explicitly_confirmed=True,
    )
    refreshed = inspect_supplier_source("supplier.csv", new_offer)
    verified = reuse_supplier_profile(
        reviewed_mapping, refreshed, "supplier.csv", new_offer,
        organization_id=ORG, supplier_id="office_vendor",
    )
    assert verified.staging.rows[0].price_rub is not None
    assert str(verified.staging.rows[0].price_rub) == "1699.00"
    # Canonical taxonomy attributes and unit/pack rules are independently
    # approved by the reviewer, NOT inferred from names or from the LLM.
    decision_set = _decisions(verified.staging)
    canonical = _render(verified.staging, decision_set)
    assert canonical.validation.publishable
    assert [p.sku for p in canonical.validation.products] == ["K-001", "M-002"]
    assert str(canonical.validation.products[0].price_rub) == "1699.00"
    assert canonical.products_count == 2
    assert canonical.excluded_source_rows == ()
    assert canonical.published is False


def test_source_pack_price_cannot_silently_become_per_piece_price() -> None:
    staging = stage_supplier_prices(
        "supplier.csv",
        "sku;name;price;currency;unit;pack\n"
        "CLIP-12;Clips pack;240.00;RUB;уп;12\n".encode(),
        SupplierMapping(
            sku=0, name=1, price=2, currency=3,
            sale_unit=4, pieces_per_sale_unit=5, header_row=1,
        ),
    )
    row = staging.rows[0]
    decision = ReviewedProductDecision(
        line=row.line,
        category="stationery_other",
        sale_unit="штука",
        pieces_per_sale_unit=1,
        source_row_sha256=source_row_sha256(row),
        confirmed=True,
        attributes={"product_kind": "item"},
    )
    with pytest.raises(CanonicalExportRejected, match="price_unit_basis_requires_review"):
        _render(staging, (decision,))
    with pytest.raises(CanonicalExportRejected, match="price_unit_basis_requires_review"):
        _render(staging, (
            replace(decision, price_unit_review_evidence="checked"),
        ))
    documented = replace(
        decision,
        price_unit_review_evidence=(
            "Human separately confirmed from supplier terms that quoted "
            "240 RUB is per one item, not per 12-item package."
        ),
    )
    result = _render(staging, (documented,))
    assert result.validation.publishable
    assert result.validation.products[0].sale_unit == "штука"
    assert str(result.validation.products[0].price_rub) == "240.00"
    assert result.published is False


def test_source_units_and_package_quantity_preserved_when_semantics_match() -> None:
    staging = stage_supplier_prices(
        "supplier.csv",
        "sku;name;price;currency;unit;pack\n"
        "CLIP-12;Clips pack;240.00;RUB;уп;12\n".encode(),
        SupplierMapping(
            sku=0, name=1, price=2, currency=3,
            sale_unit=4, pieces_per_sale_unit=5, header_row=1,
        ),
    )
    row = staging.rows[0]
    matching = ReviewedProductDecision(
        line=row.line, category="stationery_other",
        sale_unit="пачка", pieces_per_sale_unit=12,
        source_row_sha256=source_row_sha256(row),
        confirmed=True, attributes={"product_kind": "item"},
    )
    matching_result = _render(staging, (matching,))
    assert matching_result.validation.publishable
    assert matching_result.validation.products[0].pieces_per_sale_unit == 12
    with pytest.raises(CanonicalExportRejected, match="price_unit_basis_requires_review"):
        _render(staging, (
            replace(matching, pieces_per_sale_unit=1),
        ))


def test_unknown_vendor_sale_units_require_price_basis_evidence() -> None:
    staging = stage_supplier_prices(
        "supplier.csv",
        "sku;name;price;currency;unit;pack\n"
        "WIRE-1;Wire;240.00;RUB;м;1\n".encode(),
        SupplierMapping(
            sku=0, name=1, price=2, currency=3,
            sale_unit=4, pieces_per_sale_unit=5, header_row=1,
        ),
    )
    row = staging.rows[0]
    decision = ReviewedProductDecision(
        line=row.line, category="stationery_other",
        sale_unit="штука", pieces_per_sale_unit=1,
        source_row_sha256=source_row_sha256(row),
        confirmed=True, attributes={"product_kind": "item"},
    )
    with pytest.raises(CanonicalExportRejected, match="price_unit_basis_requires_review"):
        _render(staging, (decision,))


def test_missing_source_unit_with_multi_piece_package_requires_price_evidence() -> None:
    staging = stage_supplier_prices(
        "supplier.csv",
        b"sku;name;price;currency;unit;pack\n"
        b"CLIP-12;Clips pack;240.00;RUB;;12\n",
        SupplierMapping(
            sku=0, name=1, price=2, currency=3,
            sale_unit=4, pieces_per_sale_unit=5, header_row=1,
        ),
    )
    row = staging.rows[0]
    decision = ReviewedProductDecision(
        line=row.line, category="stationery_other",
        sale_unit="штука", pieces_per_sale_unit=1,
        confirmed=True, source_row_sha256=source_row_sha256(row),
        attributes={"product_kind": "item"},
    )
    with pytest.raises(CanonicalExportRejected, match="price_unit_basis_requires_review"):
        _render(staging, (decision,))
