"""Client offer alternatives use the saved request and real immutable price list."""
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from typing import Literal

import pytest
from pypdf import PdfReader
from test_commercial_document import content
from test_composition_variants import candidate, item

from kpdesk.extraction.schema import ExtractedRequest
from kpdesk.matching.schema import CatalogProduct
from kpdesk.quotes.alternatives import available_alternatives, resolve_alternatives
from kpdesk.quotes.document import AlternativeSelection, DocumentApproval
from kpdesk.quotes.pdf import render_draft_pdf
from kpdesk.quotes.storage import ManagerSkuSelection, StoredQuote, _make_snapshot


def saved() -> tuple[StoredQuote, list[CatalogProduct]]:
    products = [candidate('FIRST', '10.01', .9).product,
                candidate('SECOND', '6.00', .8).product]
    requested = item()
    extracted = ExtractedRequest.model_validate({'lines': [{
        **requested.model_dump(), 'source_quote': requested.description,
    }]})
    snapshot = _make_snapshot(extracted, products, [ManagerSkuSelection(line_number=1, sku='FIRST')])
    return StoredQuote(quote_id=uuid.uuid4(), revision=1, catalog_version_id=uuid.uuid4(),
                       snapshot_sha256='a' * 64, **snapshot.model_dump(exclude={'lines', 'source_request', 'instructions', 'document', 'approval'}),
                       lines=snapshot.lines, source_request=extracted), products


def test_resolves_real_lines_and_rejects_duplicate_primary_and_partial_selections() -> None:
    quote, products = saved()
    selection = AlternativeSelection(kind='lower_cost', selections=(ManagerSkuSelection(line_number=1, sku='SECOND'),))
    resolved = resolve_alternatives(quote, products, [selection])
    assert resolved[0].total_rub == Decimal('60.00')
    assert resolved[0].lines[0].unit_price_rub == Decimal('6.00')
    assert resolved[0].catalog_version_id == quote.catalog_version_id
    with pytest.raises(ValueError, match='duplicate_composition'):
        resolve_alternatives(quote, products, [selection, selection])
    with pytest.raises(ValueError, match='duplicate_composition'):
        resolve_alternatives(quote, products, [AlternativeSelection(
            kind='relevance', selections=(ManagerSkuSelection(line_number=1, sku='FIRST'),))])
    with pytest.raises(ValueError):
        resolve_alternatives(quote, products, [AlternativeSelection(
            kind='lower_cost', selections=(ManagerSkuSelection(line_number=1, sku='MISSING'),))])


def test_preview_deduplicates_against_primary_and_legacy_quotes_have_no_silent_source() -> None:
    quote, products = saved()
    variants = available_alternatives(quote, products)
    assert variants and all(v.lines[0].sku != 'FIRST' for v in variants)
    with pytest.raises(ValueError, match='alternative_source_request_unavailable'):
        available_alternatives(replace(quote, source_request=None), products)
    assert 'alternatives' not in content().model_dump(mode='json')


def test_pdf_keeps_separate_totals_and_saved_alternative_prices_without_catalog_reads() -> None:
    quote, products = saved()
    alternative = resolve_alternatives(quote, products, [AlternativeSelection(
        kind='lower_cost', selections=(ManagerSkuSelection(line_number=1, sku='SECOND'),))])[0]
    document = content().model_copy(update={'alternatives': (alternative,)})
    approved = replace(quote, revision=2, approved=True, document=document,
                       approval=DocumentApproval(user_id=uuid.uuid4(), approved_at=datetime.now(UTC),
                                                 source_revision=1, source_snapshot_sha256=quote.snapshot_sha256))
    pdf = render_draft_pdf(approved)
    text = '\n'.join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages)
    assert 'Вариант 2' in text and 'SECOND' in text and 'FIRST' in text
    assert '100.10' in text and '60.00' in text and '40.10' in text
    assert '160.10' not in text
    assert 'Закупка' not in text
    assert 'Что меняется' in text
    assert 'Без замены: 0 из 1' in text
    assert 'Количество и единица продажи сохранены: 10' in text


def test_llm_receives_selected_alternative_goods_without_procurement_prices() -> None:
    import json

    from kpdesk.quotes.narrative import proposal_facts

    quote, products = saved()
    alternatives = available_alternatives(quote, products)
    facts = proposal_facts(quote, issuer_name='Компания', recipient_name='',
                           manager_brief='Предложи выбор состава', commercial_terms='',
                           alternatives=alternatives)
    payload = json.loads(facts)
    assert payload['alternatives'][0]['variant_number'] == 2
    assert payload['alternatives'][0]['products'][0]['quantity'] == 10
    assert 'unit_price_rub' not in facts and '60.00' not in facts and '10.01' not in facts


def test_resolution_does_not_bypass_confirmed_budget_or_missing_rows() -> None:
    from kpdesk.quotes.instructions import ConfirmedInstructions, propose_instructions

    quote, products = saved()
    policy = propose_instructions('Бюджет до 50 рублей.')
    restricted = replace(quote, instructions=ConfirmedInstructions(raw_text=policy.raw_text, rules=policy.rules))
    with pytest.raises(ValueError, match='alternative_not_complete_or_over_budget'):
        resolve_alternatives(restricted, products, [AlternativeSelection(
            kind='lower_cost', selections=(ManagerSkuSelection(line_number=1, sku='SECOND'),))])
    with pytest.raises(ValueError, match='incomplete_alternative_selection'):
        resolve_alternatives(quote, products, [AlternativeSelection(
            kind='lower_cost', selections=(ManagerSkuSelection(line_number=2, sku='SECOND'),))])


def test_reversed_selection_order_is_rejected_before_document_approval() -> None:
    quote, products = saved()
    assert quote.source_request is not None
    source = quote.source_request.model_copy(update={'lines': quote.source_request.lines * 2})
    main = _make_snapshot(source, products, [ManagerSkuSelection(line_number=n, sku='FIRST') for n in (1, 2)])
    quote = replace(quote, lines=main.lines, source_request=source, total_rub=main.total_rub)
    reversed_selection = AlternativeSelection(kind='lower_cost', selections=(
        ManagerSkuSelection(line_number=2, sku='SECOND'), ManagerSkuSelection(line_number=1, sku='SECOND'),
    ))
    with pytest.raises(ValueError, match='incomplete_alternative_selection'):
        resolve_alternatives(quote, products, [reversed_selection])


@pytest.mark.parametrize('template', [None, 'presentation', 'compact'])
def test_twenty_line_three_variant_pdf_keeps_text_within_pages(template: str | None) -> None:
    import pymupdf

    quote, products = saved()
    products.append(candidate('THIRD', '12.00', .7).product)
    assert quote.source_request is not None
    source = quote.source_request.model_copy(update={'lines': tuple(
        quote.source_request.lines[0] for _ in range(20)
    )})
    main = _make_snapshot(source, products, [ManagerSkuSelection(line_number=n, sku='FIRST') for n in range(1, 21)])
    quote = replace(quote, lines=main.lines, total_rub=main.total_rub, source_request=source,
                    base_total_rub=main.base_total_rub, estimated_total_rub=main.estimated_total_rub)
    choices: tuple[tuple[Literal['lower_cost', 'relevance'], str], ...] = (('lower_cost', 'SECOND'), ('relevance', 'THIRD'))
    alternatives = resolve_alternatives(quote, products, [AlternativeSelection(
        kind=kind, selections=tuple(ManagerSkuSelection(line_number=n, sku=sku) for n in range(1, 21)),
    ) for kind, sku in choices])
    from kpdesk.quotes.appearance import DocumentAppearance

    appearance = DocumentAppearance.model_validate({'template': template}) if template else None
    approved = replace(quote, revision=2, approved=True,
                       document=content().model_copy(update={'alternatives': alternatives, 'appearance': appearance}),
                       approval=DocumentApproval(user_id=uuid.uuid4(), approved_at=datetime.now(UTC),
                                                 source_revision=1, source_snapshot_sha256=quote.snapshot_sha256))
    raw = render_draft_pdf(approved)
    with pymupdf.open(stream=raw, filetype='pdf') as pdf:  # type: ignore[no-untyped-call]
        assert len(pdf) >= 3
        text = '\n'.join(page.get_text() for page in pdf)
        assert 'Вариант 3' in text and '2400.00' in text and '1200.00' in text
        for page in pdf:
            for x0, y0, x1, y1, *_ in page.get_text('blocks'):
                assert x0 >= 0 and y0 >= 0 and x1 <= page.rect.width + 1 and y1 <= page.rect.height + 1
