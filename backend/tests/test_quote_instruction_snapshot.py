"""Persisted snapshots with confirmed rules must retain exact buying/selling arithmetic."""
from __future__ import annotations

import uuid
from dataclasses import replace
from decimal import Decimal
from io import BytesIO

import pytest
from pypdf import PdfReader
from test_auto_draft_api import extracted

from kpdesk.extraction.schema import ExtractedRequest
from kpdesk.matching.schema import CatalogProduct
from kpdesk.quotes.instructions import ConfirmedInstructions, propose_instructions
from kpdesk.quotes.pdf import QuotePDFError, render_draft_pdf
from kpdesk.quotes.storage import QuoteSnapshot, StoredQuote, _digest, _hash_request, _make_snapshot


def _fixture(
    text: str, *, brand: str | None = None,
) -> tuple[ConfirmedInstructions, ExtractedRequest, QuoteSnapshot]:
    rule_preview = propose_instructions(text)
    assert rule_preview.can_confirm, rule_preview.unresolved_requirements
    instructions = ConfirmedInstructions(raw_text=text, rules=rule_preview.rules)
    request = ExtractedRequest.model_validate({"lines": [
        extracted(
            "pencil", "Карандаш графитовый HB",
            {"pencil_type": "graphite", "lead_grade": "HB"}, 10,
        ),
    ]})
    product = CatalogProduct(
        sku="PEN-1", name="Карандаш графитовый HB", category="pencil",
        sale_unit="штука", pieces_per_sale_unit=1, price_rub=Decimal("19.90"),
        brand=brand, attributes={"pencil_type": "graphite", "lead_grade": "HB"},
    )
    snapshot = _make_snapshot(request, [product], instructions=instructions)
    return instructions, request, snapshot


def test_versioned_pricing_snapshot_preserves_base_and_exact_rules() -> None:
    rules, extracted_request, snapshot = _fixture("Наценка 18%. Общий бюджет — до 300 000 рублей.")
    assert snapshot.ready_for_draft
    assert snapshot.base_total_rub == Decimal("199.00")
    assert snapshot.total_rub == Decimal("234.82")
    assert snapshot.lines[0].unit_price_rub == Decimal("19.90")
    assert snapshot.lines[0].line_total_rub == Decimal("234.82")
    assert snapshot.instructions == rules
    restored = type(snapshot).model_validate(snapshot.model_dump(mode="json"))
    assert restored == snapshot
    assert _digest(restored.model_dump(mode="json")) == _digest(snapshot.model_dump(mode="json"))
    catalog_id = uuid.uuid4()
    assert _hash_request(catalog_id, extracted_request, None, None, None, rules) != (
        _hash_request(catalog_id, extracted_request, None, None)
    )


def test_pricing_snapshot_blocks_budget_and_pdf_for_overbudget_revision() -> None:
    rules, _, snapshot = _fixture("Наценка 18%. Бюджет до 220 рублей.")
    assert rules.rules.budget_max_rub == Decimal(220)
    assert snapshot.budget_exceeded
    assert snapshot.total_rub is None
    assert snapshot.estimated_total_rub == Decimal("234.82")
    assert snapshot.ready_for_draft is False
    quote = StoredQuote(
        quote_id=uuid.uuid4(), revision=1, catalog_version_id=uuid.uuid4(),
        ready_for_draft=snapshot.ready_for_draft, total_rub=snapshot.total_rub,
        lines=snapshot.lines, snapshot_sha256="a" * 64,
        instructions=rules, base_total_rub=snapshot.base_total_rub,
        estimated_total_rub=snapshot.estimated_total_rub,
        budget_exceeded=snapshot.budget_exceeded,
    )
    with pytest.raises(QuotePDFError, match="incomplete_draft"):
        render_draft_pdf(quote)


def test_ambiguous_budget_cannot_create_a_new_priced_revision() -> None:
    valid, request, _ = _fixture('Наценка 18%. Бюджет до 1234 рублей.')
    ambiguous = ConfirmedInstructions(
        raw_text='Наценка 18%. Бюджет до 12 34 рублей.', rules=valid.rules,
    )
    product = CatalogProduct(
        sku='PEN-1', name='Карандаш графитовый HB', category='pencil',
        sale_unit='штука', pieces_per_sale_unit=1, price_rub=Decimal('19.90'),
        attributes={'pencil_type': 'graphite', 'lead_grade': 'HB'},
    )
    with pytest.raises(ValueError, match='instruction_confirmation_mismatch'):
        _make_snapshot(request, [product], instructions=ambiguous)


def test_rules_pdf_is_generated_from_saved_markup_and_denies_tampering() -> None:
    rules, _, snapshot = _fixture("Наценка 18%. Бюджет до 300 рублей.")
    quote = StoredQuote(
        quote_id=uuid.uuid4(), revision=1, catalog_version_id=uuid.uuid4(),
        ready_for_draft=snapshot.ready_for_draft, total_rub=snapshot.total_rub,
        lines=snapshot.lines, snapshot_sha256="a" * 64,
        instructions=rules, base_total_rub=snapshot.base_total_rub,
        estimated_total_rub=snapshot.estimated_total_rub,
        budget_exceeded=snapshot.budget_exceeded,
    )
    raw = render_draft_pdf(quote)
    assert raw.startswith(b"%PDF-")
    body = PdfReader(BytesIO(raw)).pages[0].extract_text()
    assert "234.82" in body and "19.90" in body and "18%" in body
    tampered = replace(quote, total_rub=Decimal("239.00"))
    with pytest.raises(QuotePDFError, match="inconsistent_quote_total"):
        render_draft_pdf(tampered)


def test_pdf_verifies_and_displays_immutable_brand_policy() -> None:
    instructions, _, snapshot = _fixture(
        "Только бренд DemoPaper. Наценка 18%.", brand="DemoPaper",
    )
    assert snapshot.ready_for_draft
    assert snapshot.lines[0].product_brand == "DemoPaper"
    roundtrip = QuoteSnapshot.model_validate(snapshot.model_dump(mode="json"))
    assert roundtrip.lines[0].product_brand == "DemoPaper"
    quote = StoredQuote(
        quote_id=uuid.uuid4(), revision=1, catalog_version_id=uuid.uuid4(),
        ready_for_draft=snapshot.ready_for_draft, total_rub=snapshot.total_rub,
        lines=snapshot.lines, snapshot_sha256=_digest(snapshot.model_dump(mode="json")),
        instructions=instructions, base_total_rub=snapshot.base_total_rub,
    )
    pdf = PdfReader(BytesIO(render_draft_pdf(quote))).pages[0].extract_text()
    assert "demopaper" in pdf
    assert "234.82" in pdf
    changed_line = quote.lines[0].model_copy(update={"product_brand": "OtherBrand"})
    with pytest.raises(QuotePDFError, match="inconsistent_quote_brand_policy"):
        render_draft_pdf(replace(quote, lines=(changed_line,)))
