"""Deterministic offer comparison on independent immutable supplier-source identities."""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from kpdesk.extraction.schema import ExtractedRequest
from kpdesk.matching.schema import CatalogProduct
from kpdesk.quotes.instructions import ConfirmedInstructions, propose_instructions
from kpdesk.quotes.storage import LockedQuoteLine, QuoteSnapshot
from kpdesk.quotes.supplier_comparison import (
    SupplierOfferSelection,
    VerifiedSupplierCatalog,
    compare_supplier_catalogs,
    lock_supplier_selection,
)


def _catalog(
    supplier_id: int, *, price: str, brand: str | None = "PaperCo",
    name: str = "Карандаш графитовый HB", sku: str = "SAME-SKU",
    category: str = "pencil",
) -> VerifiedSupplierCatalog:
    return VerifiedSupplierCatalog(
        supplier_id=f"supplier_{supplier_id}",
        supplier_name=f"Поставщик {supplier_id}",
        catalog_version_id=uuid.UUID(int=supplier_id + 100),
        source_sha256=f"{supplier_id:064x}",
        products=(CatalogProduct(
            sku=sku, name=name, category=category,
            sale_unit="штука", pieces_per_sale_unit=1,
            price_rub=Decimal(price), brand=brand,
            attributes={"pencil_type": "graphite", "lead_grade": "HB"} if category == "pencil" else {},
        ),),
    )


def _request(*, description: str = "Карандаш графитовый HB") -> ExtractedRequest:
    return ExtractedRequest.model_validate({"lines": [{
        "source_quote": description, "description": description,
        "category": "pencil", "quantity": 2, "unit": "штука",
        "brand": None, "paper_format": None, "paper_gsm": None,
        "pen_ink_color": None, "pen_ink_type": None,
        "attributes": {"pencil_type": "graphite", "lead_grade": "HB"},
    }]})


def _confirmed(text: str) -> ConfirmedInstructions:
    preview = propose_instructions(text)
    assert preview.can_confirm
    return ConfirmedInstructions(raw_text=preview.raw_text, rules=preview.rules)


def test_same_supplier_sku_has_independent_price_and_version_provenance() -> None:
    request = _request()
    sources = (_catalog(1, price="30.00"), _catalog(2, price="19.90"))
    compared = compare_supplier_catalogs(request, sources)
    assert compared.requires_manager_confirmation
    assert compared.catalog_source_count == 2
    line = compared.lines[0]
    assert line.status == "select_supplier"
    assert [offer.product.sku for offer in line.offers] == ["SAME-SKU", "SAME-SKU"]
    assert [offer.supplier_id for offer in line.offers] == [
        sources[1].supplier_id, sources[0].supplier_id,
    ]
    assert [offer.base_total_rub for offer in line.offers] == [
        Decimal("39.80"), Decimal("60.00"),
    ]
    unresolved = lock_supplier_selection(request, compared, ())
    assert not unresolved.ready_for_draft
    assert unresolved.total_rub is None
    assert unresolved.lines[0].reason_codes == ("supplier_selection_required",)

    selected = lock_supplier_selection(request, compared, (SupplierOfferSelection(
        line_number=1, supplier_id=sources[1].supplier_id,
        catalog_version_id=sources[1].catalog_version_id, sku="SAME-SKU",
    ),))
    assert selected.ready_for_draft
    assert selected.total_rub == Decimal("39.80")
    assert selected.base_total_rub == Decimal("39.80")
    locked_line = selected.lines[0]
    assert locked_line.supplier_id == sources[1].supplier_id
    assert locked_line.source_catalog_version_id == sources[1].catalog_version_id
    assert locked_line.source_sha256 == sources[1].source_sha256
    assert locked_line.unit_price_rub == Decimal("19.90")
    assert QuoteSnapshot.model_validate(selected.model_dump(mode="json")) == selected


def test_brand_rules_and_budget_are_enforced_for_every_supplier() -> None:
    sources = (
        _catalog(1, price="1.00", brand="Forbidden"),
        _catalog(2, price="20.00", brand="PaperCo"),
    )
    rules = _confirmed("Исключи бренд Forbidden. Наценка 10%. Бюджет до 40 рублей.")
    request = _request()
    compared = compare_supplier_catalogs(request, sources, rules)
    assert [offer.product.brand for offer in compared.lines[0].offers] == ["PaperCo"]
    assert compared.lines[0].offers[0].selling_total_rub == Decimal("44.00")
    with pytest.raises(ValueError, match="supplier_selection_not_in_verified_offers"):
        lock_supplier_selection(request, compared, (SupplierOfferSelection(
            line_number=1, supplier_id=sources[0].supplier_id,
            catalog_version_id=sources[0].catalog_version_id, sku="SAME-SKU",
        ),), rules)
    chosen = (SupplierOfferSelection(
        line_number=1, supplier_id=sources[1].supplier_id,
        catalog_version_id=sources[1].catalog_version_id, sku="SAME-SKU",
    ),)
    saved = lock_supplier_selection(request, compared, chosen, rules)
    assert saved.budget_exceeded
    assert not saved.ready_for_draft
    assert saved.total_rub is None
    assert saved.estimated_total_rub == Decimal("44.00")
    assert saved.base_total_rub == Decimal("40.00")
    assert saved.lines[0].line_total_rub == Decimal("44.00")


def test_mismatched_identity_rejected_before_price_comparison() -> None:
    sources = (
        _catalog(1, price="0.01", name="Карандаш графитовый 2B"),
        _catalog(2, price="10.00"),
    )
    compared = compare_supplier_catalogs(_request(), sources)
    assert [offer.supplier_id for offer in compared.lines[0].offers] == [
        sources[1].supplier_id,
    ]


def test_duplicate_versions_or_suppliers_are_not_distinct_evidence() -> None:
    one = _catalog(1, price="10.00")
    two = _catalog(2, price="9.00")
    request = _request()
    with pytest.raises(ValueError, match="duplicate_supplier_identity"):
        compare_supplier_catalogs(request, (one, one))
    copied = VerifiedSupplierCatalog(
        supplier_id=two.supplier_id, supplier_name=two.supplier_name,
        catalog_version_id=one.catalog_version_id,
        source_sha256=two.source_sha256, products=two.products,
    )
    with pytest.raises(ValueError, match="duplicate_supplier_catalog_version"):
        compare_supplier_catalogs(request, (one, copied))
    with pytest.raises(ValueError, match="supplier_count_out_of_range"):
        compare_supplier_catalogs(request, (one,))


def test_stale_request_or_instruction_change_rejects_selected_comparison() -> None:
    request = _request()
    sources = (_catalog(1, price="12.00"), _catalog(2, price="14.00"))
    rules = _confirmed("Наценка 10%.")
    compared = compare_supplier_catalogs(request, sources, rules)
    selection = (SupplierOfferSelection(
        line_number=1, supplier_id=sources[0].supplier_id,
        catalog_version_id=sources[0].catalog_version_id, sku="SAME-SKU",
    ),)
    with pytest.raises(ValueError, match="comparison_request_mismatch"):
        lock_supplier_selection(
            _request(description="Карандаш графитовый 2B"), compared, selection, rules,
        )
    with pytest.raises(ValueError, match="comparison_instructions_mismatch"):
        lock_supplier_selection(request, compared, selection, _confirmed("Наценка 11%."))
    with pytest.raises(ValueError, match="comparison_instructions_mismatch"):
        lock_supplier_selection(request, compared, selection)
    with pytest.raises(ValueError, match="invalid_supplier_selection_line"):
        lock_supplier_selection(request, compared, selection + selection, rules)


def test_supplier_snapshot_rejects_partial_or_false_provenance() -> None:
    data = {
        "line_number": 1, "description": "A", "status": "manager_selected",
        "sku": "X", "product_name": "A", "requested_quantity": 1,
        "requested_unit": "штука", "sale_quantity": 1, "sale_unit": "штука",
        "unit_price_rub": "10.00", "line_total_rub": "10.00", "reason_codes": [],
        "supplier_id": "supplier_verified",
    }
    with pytest.raises(ValidationError, match="incomplete_supplier_provenance"):
        LockedQuoteLine.model_validate(data)
    data.update({
        "source_catalog_version_id": str(uuid.uuid4()), "source_sha256": "f" * 64,
        "status": "no_match",
    })
    with pytest.raises(ValidationError, match="unresolved_line_cannot_claim_supplier"):
        LockedQuoteLine.model_validate(data)
    historical = dict(data)
    for key in ("supplier_id", "source_catalog_version_id", "source_sha256"):
        historical.pop(key)
    historical["status"] = "manager_selected"
    assert LockedQuoteLine.model_validate(historical).supplier_id is None


def test_one_quote_combines_distinct_suppliers_on_distinct_lines() -> None:
    """A supplier-local SKU is never treated as a globally unique price key."""
    first = _catalog(1, price="30.00", brand="PaperCo", sku="PENCIL")
    second = _catalog(2, price="19.90", brand="PaperCo", sku="PENCIL")
    next_product = second.products[0].model_copy(update={
        "name": "Карандаш графитовый 2B",
        "attributes": {"pencil_type": "graphite", "lead_grade": "2B"},
    })
    second = VerifiedSupplierCatalog(
        supplier_id=second.supplier_id, supplier_name=second.supplier_name,
        catalog_version_id=second.catalog_version_id,
        source_sha256=second.source_sha256, products=(next_product,),
    )
    source = _request().lines[0]
    next_line = source.model_copy(update={
        "source_quote": "Карандаш графитовый 2B",
        "description": "Карандаш графитовый 2B",
        "attributes": {"pencil_type": "graphite", "lead_grade": "2B"},
    })
    request = ExtractedRequest(lines=[source, next_line])
    compared = compare_supplier_catalogs(request, (first, second))
    assert [len(line.offers) for line in compared.lines] == [1, 1]
    assert compared.lines[0].offers[0].supplier_id == first.supplier_id
    assert compared.lines[1].offers[0].supplier_id == second.supplier_id
    snapshot = lock_supplier_selection(request, compared, (
        SupplierOfferSelection(1, first.supplier_id, first.catalog_version_id, "PENCIL"),
        SupplierOfferSelection(2, second.supplier_id, second.catalog_version_id, "PENCIL"),
    ))
    assert snapshot.ready_for_draft
    assert snapshot.total_rub == Decimal("99.80")
    assert {line.supplier_id for line in snapshot.lines} == {
        first.supplier_id, second.supplier_id,
    }
    assert [line.source_catalog_version_id for line in snapshot.lines] == [
        first.catalog_version_id, second.catalog_version_id,
    ]
    assert QuoteSnapshot.model_validate(snapshot.model_dump(mode="json")) == snapshot


def test_draft_pdf_preserves_line_level_supplier_and_catalog_provenance() -> None:
    from io import BytesIO

    from pypdf import PdfReader

    from kpdesk.quotes.pdf import render_draft_pdf
    from kpdesk.quotes.storage import StoredQuote, _digest

    request = _request()
    first = _catalog(1, price="29.90")
    second = _catalog(2, price="20.00")
    comparison = compare_supplier_catalogs(request, (first, second))
    snapshot = lock_supplier_selection(request, comparison, (SupplierOfferSelection(
        1, second.supplier_id, second.catalog_version_id, "SAME-SKU",
    ),))
    stored = StoredQuote(
        quote_id=uuid.uuid4(), revision=1,
        catalog_version_id=first.catalog_version_id,
        ready_for_draft=snapshot.ready_for_draft,
        total_rub=snapshot.total_rub,
        lines=snapshot.lines,
        snapshot_sha256=_digest(snapshot.model_dump(mode="json")),
        source_request=snapshot.source_request,
        instructions=snapshot.instructions,
        base_total_rub=snapshot.base_total_rub,
    )
    text = " ".join(page.extract_text() for page in PdfReader(
        BytesIO(render_draft_pdf(stored)),
    ).pages)
    assert "supplier_2" in text
    assert str(second.catalog_version_id) in text
    assert second.source_sha256 in text
    assert "Основной каталог" in text


def test_verified_offer_matrix_is_bounded_without_discarding_candidates() -> None:
    first = _catalog(1, price="10.00")
    second = _catalog(2, price="12.00")
    first = VerifiedSupplierCatalog(
        supplier_id=first.supplier_id,
        supplier_name=first.supplier_name,
        catalog_version_id=first.catalog_version_id,
        source_sha256=first.source_sha256,
        products=tuple(
            first.products[0].model_copy(update={"sku": f"FIRST-{index:03d}"})
            for index in range(51)
        ),
    )
    second = VerifiedSupplierCatalog(
        supplier_id=second.supplier_id,
        supplier_name=second.supplier_name,
        catalog_version_id=second.catalog_version_id,
        source_sha256=second.source_sha256,
        products=tuple(
            second.products[0].model_copy(update={"sku": f"SECOND-{index:03d}"})
            for index in range(51)
        ),
    )
    with pytest.raises(ValueError, match="too_many_verified_supplier_offers"):
        compare_supplier_catalogs(_request(), (first, second))
