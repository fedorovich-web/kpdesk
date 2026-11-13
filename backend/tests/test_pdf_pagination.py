"""Actual PDF pagination, using a frozen synthetic proposal with saved model text."""
import json
from dataclasses import replace
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest
from pypdf import PdfReader

from kpdesk.quotes.document import CommercialDocument
from kpdesk.quotes.line import LockedQuoteLine
from kpdesk.quotes.pdf import render_document_preview
from kpdesk.quotes.storage import StoredQuote


def proposal() -> tuple[StoredQuote, CommercialDocument]:
    fixture = json.loads((Path(__file__).parent / "fixtures/proposal_pagination.json").read_text(encoding="utf-8"))
    values = fixture["fixed_quote"]
    quote = StoredQuote(
        quote_id=UUID(values["quote_id"]), revision=values["revision"],
        catalog_version_id=UUID(values["catalog_version_id"]),
        ready_for_draft=values["ready_for_draft"], total_rub=Decimal(values["total_rub"]),
        lines=tuple(LockedQuoteLine.model_validate(line) for line in values["lines"]),
        snapshot_sha256=values["snapshot_sha256"],
    )
    return quote, CommercialDocument.model_validate(fixture["document"])


def test_presentation_preserves_complete_content_across_pages() -> None:
    quote, document = proposal()
    raw = render_document_preview(quote, document)
    pages = PdfReader(BytesIO(raw)).pages
    # Page count follows content volume; all saved facts must survive pagination.
    assert len(pages) >= 1
    text = "\n".join(page.extract_text() for page in pages)
    normalized = " ".join(text.split())
    assert "\x00" not in text, "The selected PDF font must preserve text glyphs."
    assert "1128.50 ₽" in normalized
    for line in quote.lines:
        assert line.product_name is not None
        assert " ".join(line.product_name.split()) in normalized
    assert "Контакты" in text and "acceptance@example.invalid" in text
    assert "Просим подтвердить" in text and "1128.50" in text
    assert "НЕ УТВЕРЖДЕНО" in text
    assert render_document_preview(quote, document) == raw


@pytest.mark.parametrize("paragraphs", [2, 6, 12])
def test_composition_heading_stays_with_first_row_when_prose_grows(paragraphs: int) -> None:
    quote, document = proposal()
    narrative = document.narrative.model_copy(update={
        "solution": "\n".join([document.narrative.solution] * paragraphs),
    })
    pages = PdfReader(BytesIO(render_document_preview(
        quote, document.model_copy(update={"narrative": narrative}),
    ))).pages
    for page in pages:
        text = " ".join(page.extract_text().split())
        if "Состав предложения" in text:
            assert "Бумага офисная" in text and "699.80" in text


@pytest.mark.parametrize("template", ["presentation", "compact", None])
def test_long_offer_preserves_rows_sections_and_repeats_table_header(template: str | None) -> None:
    quote, document = proposal()
    source = quote.lines[0]
    lines = tuple(source.model_copy(update={
        "line_number": i, "sku": f"PG-{i:02}",
        "product_name": f"Позиция {i}: бумага для офисной печати А4 80 г/м², 500 листов",
    }) for i in range(1, 21))
    quote = replace(quote, lines=lines, total_rub=Decimal("699.80") * 20)
    appearance = document.appearance
    assert appearance is not None
    document = document.model_copy(update={
        "appearance": appearance.model_copy(update={"template": template}) if template else None,
    })
    pages = PdfReader(BytesIO(render_document_preview(quote, document))).pages
    assert len(pages) >= 2
    if template:
        assert "PG-01" in pages[0].extract_text(), "A long table must use the space below its introduction."
    text = "\n".join(page.extract_text() for page in pages)
    assert "₽" in text and "\x00" not in text
    for i in range(1, 21):
        assert f"PG-{i:02}" in text
    assert text.count("Наименование") >= 2
    assert "13996.00" in text
    assert document.narrative.closing in " ".join(text.split())
    assert "acceptance@example.invalid" in text
    for page in pages:
        page_text = page.extract_text()
        if "Контакты" in page_text:
            assert "acceptance@example.invalid" in page_text
